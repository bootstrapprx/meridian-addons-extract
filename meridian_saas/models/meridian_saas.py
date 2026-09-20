import logging
import re
import secrets
from contextlib import closing
from psycopg2 import IntegrityError
import odoo
from odoo import _, api, fields, models, modules
from odoo.exceptions import AccessError, UserError
from odoo.modules.registry import Registry
from odoo.service.db import DatabaseExists, _create_empty_database

_logger = logging.getLogger(__name__)

# Umbrella application carrying the whole workspace suite as dependencies.
# Its depends list is generated from backend/profiles/usgaap/profile.json by
# infra/scripts/sync-usgaap-app.py.
UMBRELLA_APP = "usgaap"

DB_NAME_RE = re.compile(r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")
# Only control-plane and PostgreSQL system databases. "md" is NOT reserved:
# it is a licensed workspace like any other (entitlement via invite-code), so
# it must be free to provision/route through the normal dedicated-db path.
# Slug collisions with existing workspaces are caught upstream (tenant lookup)
# and by the tenant_db uniqueness guard, not by a per-slug blocklist.
RESERVED_DB_NAMES = {"meridian", "postgres", "template0", "template1"}

# provision_usgaap_kit always creates a dedicated per-tenant database, so the
# backend is the authoritative source of the binding policy — it reports the
# policy in its response and the BFF persists that value verbatim (no BFF-side
# inference). One true value, one place.
PROVISIONED_BACKEND_POLICY = "dedicated_db"

class MeridianSaasRequest(models.Model):
    _name = "meridian.saas.request"
    _description = "Meridian SaaS Provisioning Request"

    request_id = fields.Char(required=True, index=True)
    tenant_id = fields.Char(required=True, index=True)
    product_key = fields.Char(required=True)
    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("completed", "Completed"),
            ("blocked", "Blocked"),
            ("failed", "Failed"),
        ],
        default="pending",
        required=True,
    )
    odoo_db = fields.Char()
    odoo_company_id = fields.Integer()
    odoo_partner_id = fields.Integer()
    odoo_user_id = fields.Integer()
    message = fields.Text()
    created_at = fields.Datetime(default=fields.Datetime.now, required=True)
    claimed_at = fields.Datetime()

    _request_id_uniq = models.Constraint(
        "UNIQUE(request_id)",
        "SaaS request_id must be unique.",
    )


class MeridianSaasEvent(models.Model):
    _name = "meridian.saas.event"
    _description = "Meridian SaaS Provisioning Event"
    _order = "created_at asc, id asc"

    request_id = fields.Char(required=True, index=True)
    event_kind = fields.Selection(
        [("info", "Info"), ("success", "Success"), ("error", "Error")],
        default="info",
        required=True,
    )
    message = fields.Text(required=True)
    created_at = fields.Datetime(default=fields.Datetime.now, required=True)


class MeridianSaas(models.AbstractModel):
    _name = "meridian.saas"
    _description = "Meridian SaaS Provisioning API"

    @api.model
    def _missing_required_modules(self):
        """Hard requirements of the GAAP kit that must be installed."""
        required = ["account", "l10n_us", "l10n_us_account", "poseidon_accounting_kernel"]
        Module = self.env["ir.module.module"].sudo()
        return [
            name
            for name in required
            if not Module.search([("name", "=", name), ("state", "=", "installed")], limit=1)
        ]

    @api.model
    def _install_umbrella_app(self, request_id):
        """Install the usgaap umbrella app to reconcile module drift.

        Returns True when an install ran (caller should re-check requirements).
        Module operations are not transactional, so this is skipped inside
        tests — core raises RuntimeError there anyway (ir_module.py).
        """
        if modules.module.current_test:
            return False
        Module = self.env["ir.module.module"].sudo()
        umbrella = Module.search([("name", "=", UMBRELLA_APP)], limit=1)
        if not umbrella:
            Module.update_list()
            umbrella = Module.search([("name", "=", UMBRELLA_APP)], limit=1)
        if not umbrella:
            self._log_event(
                request_id,
                "error",
                f"Umbrella app '{UMBRELLA_APP}' is not on the addons path; cannot self-heal modules.",
            )
            return False
        if umbrella.state == "installed":
            return False
        self._log_event(
            request_id, "info", f"Installing umbrella app '{UMBRELLA_APP}' (full workspace suite)."
        )
        umbrella.button_immediate_install()
        return True

    def _cached_result(self, req_row):
        return {
            "status": req_row.state,
            "message": "Cached execution result.",
            "odoo_db": req_row.odoo_db,
            "odoo_company_id": req_row.odoo_company_id,
            "odoo_partner_id": req_row.odoo_partner_id,
            "odoo_user_id": req_row.odoo_user_id,
            "backend_policy": PROVISIONED_BACKEND_POLICY,
        }

    @api.model
    def provision_usgaap_kit(self, payload):
        """Provisions a US GAAP workspace for a paying customer."""
        request_id = payload.get("request_id")
        tenant_id = payload.get("tenant_id")
        product_key = payload.get("product_key", "meridian")
        slug = payload.get("tenant_slug")
        
        if not request_id or not tenant_id or not slug:
            return {
                "status": "failed",
                "message": "Missing request_id, tenant_id, or tenant_slug in payload.",
            }

        if not DB_NAME_RE.match(slug) or slug in RESERVED_DB_NAMES:
            return {"status": "failed", "message": f"Invalid or reserved slug: {slug}"}

        # 1. Idempotency Check
        existing = self.env["meridian.saas.request"].sudo().search(
            [("request_id", "=", request_id)], limit=1
        )
        if existing and existing.state in ("completed", "blocked"):
            _logger.info("Found completed SaaS request_id: %s. Returning cached details.", request_id)
            return self._cached_result(existing)

        # Initialize SaaS Request tracking row
        if not existing:
            try:
                with self.env.cr.savepoint():
                    req_row = self.env["meridian.saas.request"].sudo().create({
                        "request_id": request_id,
                        "tenant_id": tenant_id,
                        "product_key": product_key,
                        "state": "pending",
                    })
            except IntegrityError:
                req_row = self.env["meridian.saas.request"].sudo().search(
                    [("request_id", "=", request_id)], limit=1
                )
        else:
            req_row = existing

        # Claim the request to avoid concurrent processing
        self.env.cr.execute(
            """UPDATE meridian_saas_request SET claimed_at = now() AT TIME ZONE 'UTC'
               WHERE request_id = %s AND state IN ('pending', 'failed')
                 AND (claimed_at IS NULL OR claimed_at < (now() AT TIME ZONE 'UTC') - interval '30 minutes')
               RETURNING id""", (request_id,))
        if not self.env.cr.fetchall():
            return {"status": "pending", "message": "Provisioning in progress."}

        # Tenant DB row guard
        tdb = self.env["meridian.saas.tenant_db"].sudo().search([("db_name", "=", slug)], limit=1)
        # "dropped" is a terminal state whose DB no longer exists, so the name is
        # free to reuse — without this a slug whose DB was ever dropped could
        # never be re-provisioned (it would fail "name in use by another request").
        if tdb and tdb.request_id != request_id and tdb.state not in ("creating", "failed", "dropped"):
            req_row.write({"state": "failed", "message": "Tenant database name in use by another request."})
            return {"status": "failed", "message": "Tenant database name in use by another request."}
        
        if not tdb:
            try:
                with self.env.cr.savepoint():
                    tdb = self.env["meridian.saas.tenant_db"].sudo().create({
                        "db_name": slug,
                        "tenant_id": tenant_id,
                        "request_id": request_id,
                        "state": "creating",
                    })
            except IntegrityError:
                tdb = self.env["meridian.saas.tenant_db"].sudo().search([("db_name", "=", slug)], limit=1)
                tdb.write({
                    "tenant_id": tenant_id,
                    "request_id": request_id,
                    "state": "creating",
                })
        else:
            tdb.write({
                "tenant_id": tenant_id,
                "request_id": request_id,
                "state": "creating",
            })

        # Deliberately expose claim before multi-minute install
        self.env.cr.commit()

        self._log_event(request_id, "info", "Starting SaaS provisioning workflow in Odoo backend.")
        return self._provision_dedicated_db(req_row, tdb, payload)

    def _provision_dedicated_db(self, req_row, tdb, payload):
        slug = tdb.db_name
        request_id = payload.get("request_id")
        try:
            try:
                _create_empty_database(slug)
            except DatabaseExists:
                self._log_event(request_id, "info", f"Database {slug} exists; resuming install.")
            self.env.cr.commit()
            registry = Registry.new(slug, update_module=True,
                                    install_modules=[UMBRELLA_APP], new_db_demo=False)
            with closing(registry.cursor()) as cr:
                env = odoo.api.Environment(cr, odoo.api.SUPERUSER_ID, {})
                if env["ir.module.module"].search_count(
                        [("name", "=", UMBRELLA_APP), ("state", "!=", "installed")]):
                    raise UserError(f"Umbrella app '{UMBRELLA_APP}' did not reach installed state in {slug}.")
                self._bootstrap_tenant_db(env, payload)
                company, partner, user = self._provision_workspace_records(env, payload, request_id)
                cr.commit()
            tdb.write({"state": "ready"})
            req_row.write({"state": "completed", "odoo_db": slug, 
                           "odoo_company_id": company.id, "odoo_partner_id": partner.id, "odoo_user_id": user.id})
            return {"status": "completed", "odoo_db": slug, "odoo_company_id": company.id,
                    "odoo_partner_id": partner.id, "odoo_user_id": user.id,
                    "backend_policy": PROVISIONED_BACKEND_POLICY, "message": "Tenant provisioned."}
        except Exception as e:
            req_row.write({"state": "failed", "message": str(e)})
            tdb.write({"state": "failed", "message": str(e)})
            return {"status": "failed", "message": str(e)}

    def _bootstrap_tenant_db(self, env, payload):
        # Randomize admin
        admin = env.ref("base.user_admin", raise_if_not_found=False)
        if admin:
            admin.write({"password": secrets.token_urlsafe(32)})

        # Copy SSO tokens
        ConfigParam = self.env["ir.config_parameter"].sudo()
        TenantConfigParam = env["ir.config_parameter"].sudo()
        
        prov_token = ConfigParam.get_param("meridian_saas.provision_token")
        if prov_token:
            TenantConfigParam.set_param("meridian_saas.provision_token", prov_token)
            
        sso_token = ConfigParam.get_param("meridian_saas.sso_token")
        if sso_token:
            TenantConfigParam.set_param("meridian_saas.sso_token", sso_token)
            
        workspace_url = ConfigParam.get_param("meridian_saas.workspace_root_domain")
        if workspace_url:
            TenantConfigParam.set_param("web.base.url", f"https://{payload.get('tenant_slug')}.{workspace_url}")
            TenantConfigParam.set_param("web.base.url.freeze", "True")

        # Rename My Company
        company_name = payload.get("company_name", payload.get("display_name"))
        main_company = env.ref("base.main_company", raise_if_not_found=False)
        currency_usd = env.ref("base.USD", raise_if_not_found=False)
        if main_company:
            main_company.write({
                "name": company_name,
                "currency_id": currency_usd.id if currency_usd else False,
            })

    def _provision_qbo_realm(self, env, company, payload, request_id):
        """Pre-create a QBO realm + mapping bound to the resolved company record.

        Idempotent on the mapping, not on the realm code: OAuth overwrites
        realm_id with Intuit's own id, so a replay that searched by SAAS_<slug>
        would miss the realm and orphan a second one.
        """
        if "qbo.realm" not in env:
            return
        mapping = env["qbo.company.mapping"].sudo().search(
            [("company_id", "=", company.id)], limit=1
        )
        if mapping:
            return
        slug = payload.get("tenant_slug", "")
        realm_code = f"SAAS_{slug}" if slug else "SAAS_DEFAULT"
        name = company.display_name
        realm = env["qbo.realm"].sudo().search([("realm_id", "=", realm_code)], limit=1)
        if not realm:
            realm = env["qbo.realm"].sudo().create({
                "name": f"{name} QBO realm",
                "realm_id": realm_code,
                "sync_mode": "upload",
                "state": "draft",
            })
        env["qbo.company.mapping"].sudo().create({
            "company_id": company.id,
            "realm_id": realm.id,
            "sync_enabled": True,
        })
        self._log_event(request_id, "info",
            f"Pre-created QBO realm {realm.realm_id} + mapping for {name}.")

    def _provision_workspace_records(self, env, payload, request_id):
        company_name = payload.get("company_name", payload.get("display_name"))
        currency_usd = env.ref("base.USD", raise_if_not_found=False)
        
        company = env["res.company"].sudo().search([("name", "=", company_name)], limit=1)
        if not company:
            self._log_event(request_id, "info", f"Creating new res.company: {company_name}")
            company = env["res.company"].sudo().create({
                "name": company_name,
                "currency_id": currency_usd.id if currency_usd else False,
            })
        else:
            self._log_event(request_id, "info", f"Reusing existing res.company: {company_name}")

        self._configure_company(company)
        # The tenant is still pristine here, so this is the one moment the
        # fallback chart can be removed with a known blast radius and no risk to
        # operator data. Do it now or the workspace is handed over carrying
        # postable accounts the kernel cannot see.
        purge = self._purge_foreign_chart(company)
        if purge.get("deleted"):
            self._log_event(
                request_id, "info",
                f"Removed {purge['deleted']} non-kernel accounts auto-loaded by Odoo's fallback chart.",
            )

        # Pre-create QBO realm + mapping — the user only needs to do OAuth.
        self._provision_qbo_realm(env, company, payload, request_id)

        owner_email = payload.get("owner_email")
        owner_name = payload.get("owner_name", "Owner")
        
        partner = env["res.partner"].sudo().search([("email", "=", owner_email)], limit=1)
        if not partner:
            self._log_event(request_id, "info", f"Creating owner res.partner: {owner_name} ({owner_email})")
            partner = env["res.partner"].sudo().create({
                "name": owner_name,
                "email": owner_email,
                "company_id": company.id,
            })
        else:
            self._log_event(request_id, "info", f"Found existing partner for: {owner_email}")

        user = env["res.users"].sudo().search([("login", "=", owner_email)], limit=1)
        if not user:
            self._log_event(request_id, "info", f"Creating owner res.users: {owner_email}")
            user = env["res.users"].sudo().create({
                "name": owner_name,
                "login": owner_email,
                "email": owner_email,
                "partner_id": partner.id,
                "company_id": company.id,
                "company_ids": [(6, 0, [company.id])],
            })
            try:
                user.sudo().action_reset_password()
            except Exception as reset_err:
                self._log_event(
                    request_id,
                    "info",
                    f"Owner invite email not sent ({reset_err}); set credentials via reset flow.",
                )
        else:
            self._log_event(request_id, "info", f"Reusing owner res.users: {owner_email}")
            portal_group = env.ref("base.group_portal", raise_if_not_found=False)
            internal_group = env.ref("base.group_user", raise_if_not_found=False)
            if portal_group and internal_group and portal_group.id in user.group_ids.ids:
                self._log_event(
                    request_id, "info",
                    f"Converting portal user {owner_email} to internal.",
                )
                user.write({
                    "group_ids": [
                        (3, portal_group.id),
                        (4, internal_group.id),
                    ],
                })
            if company.id not in user.company_ids.ids:
                self._batch_grant_company_access(user, [company.id])

        group_manager = env.ref("meridian_saas.group_meridian_saas_manager", raise_if_not_found=False)
        if group_manager:
            # group_ids, not groups_id: Odoo 19 renamed the field, and the wrong
            # name raises KeyError inside write() — which _provision_dedicated_db
            # catches and turns into a failed provisioning request.
            user.sudo().write({"group_ids": [(4, group_manager.id)]})
            self._log_event(request_id, "info", "Assigned the Meridian manager role.")

        self._log_event(request_id, "success", "Provisioning successfully finished.")
        return company, partner, user

    def _configure_company(self, company):
        """Apply Poseidon accounting configuration to a company.

        Auto-seed the kernel-required (L0) accounts so the company can post
        double-entry immediately. The fuller L1 chart is offered as a reviewable
        suggestion during onboarding (meridian_onboarding). We keep the
        "poseidon_qbo_us" label because the kernel classifier recognises it; the
        accounts themselves come from the frozen kernel master chart, so they are
        kernel-aligned regardless of the label.
        """
        # Always work through the COMPANY's own env, never self.env. At
        # provisioning time `self` is bound to the control-plane registry while
        # `company` lives in the freshly created tenant registry, so self.env
        # would resolve the chart-template model against the wrong database and
        # publish onto whatever company happens to share that id there.
        env = company.env
        accepted = env["poseidon.kernel.version"]._us_chart_template_codes()
        if company.chart_template not in accepted:
            company.chart_template = "poseidon_qbo_us"
            env["account.chart.template"].sudo().poseidon_publish_missing_standard_accounts(
                company.id,
                required_only=True,
            )

    # Canonical (kernel master-chart) replacements for the plumbing that Odoo's
    # fallback chart wires onto its own accounts. None means "clear": the kernel
    # chart has no equivalent and a workspace holding runs no such workflow.
    _FOREIGN_CHART_COMPANY_FIELDS = {
        "income_account_id": "40000",
        "expense_account_id": "60000",
        "income_currency_exchange_account_id": "40000",
        "expense_currency_exchange_account_id": "60000",
        "default_cash_difference_income_account_id": "40000",
        "default_cash_difference_expense_account_id": "60000",
        "account_journal_early_pay_discount_gain_account_id": "40000",
        "account_journal_early_pay_discount_loss_account_id": "60000",
        "transfer_account_id": "10100",
        "account_journal_suspense_account_id": "10100",
        "account_default_pos_receivable_account_id": "12000",
        "account_production_wip_account_id": None,
        "account_production_wip_overhead_account_id": None,
        "account_stock_valuation_id": None,
    }
    _FOREIGN_CHART_DEFAULTS = {
        ("product.category", "property_account_income_categ_id"): "40000",
        ("product.category", "property_account_expense_categ_id"): "60000",
        ("res.partner", "property_account_payable_id"): "20000",
        ("res.partner", "property_account_receivable_id"): "12000",
    }
    _FOREIGN_CHART_JOURNAL_DEFAULT = {"sale": "40000", "purchase": "60000", "bank": "10000"}

    @api.model
    def _template_artifacts(self, env, model, company):
        """Records of ``model`` on ``company`` that a chart template created.

        A loaded chart template registers everything it creates in
        ir.model.data (module ``account``, name ``<company_id>_...``); anything
        the operator later builds through the app carries no xmlid. Origin, not
        shape, is what makes a record safe to delete here — ``setup_taxes``
        creates real US taxes that a repair run must leave alone.
        """
        records = env[model].sudo().search([("company_id", "=", company.id)])
        if not records:
            return records
        data = env["ir.model.data"].sudo().search(
            [("model", "=", model), ("res_id", "in", records.ids)]
        )
        return records.browse(data.mapped("res_id"))

    @api.model
    def _purge_foreign_chart(self, company):
        """Delete every non-kernel account on ``company``, plus the localization
        artifacts (taxes, reconcile models) that arrived with them.

        Why this has to exist: when a module carrying account templates finishes
        installing on a company with no ``chart_template``, Odoo loads one for it
        — matching the company's country, else falling back to ``generic_coa``
        (addons/account/models/ir_module.py::write). A fresh tenant DB has
        neither country nor chart_template, so it always gets the fallback. It
        cannot be pre-empted: ``chart_template`` is defined BY the account
        module, so there is no window in which to set it first.

        Those accounts carry no ``qbo_standard_account_id``, which makes them
        postable yet invisible to every kernel surface (chart, mapping, rollup)
        — a figure could land in a money report and nowhere in the kernel
        structure. Archiving them instead of deleting is NOT an option: core's
        ``_ensure_code_is_unique`` searches with ``active_test=False``, so an
        archived account keeps reserving its code and silently blocks a later
        legitimate account with a duplicate-code error the operator cannot see
        the cause of.

        Refuses if the company has posted lines: this is meant to run on a
        pristine tenant, and no cleanup is worth destroying real accounting.
        """
        env = company.env  # see _configure_company: never self.env here
        Account = env["account.account"].sudo().with_company(company)
        foreign = Account.with_context(active_test=False).search(
            [("company_ids", "=", company.id), ("qbo_standard_account_id", "=", False)]
        )
        if not foreign:
            return {"deleted": 0}
        if env["account.move.line"].sudo().search_count([("company_id", "=", company.id)]):
            _logger.warning(
                "Refusing to purge the foreign chart on company %s: it has posted lines.", company.id
            )
            return {"deleted": 0, "skipped": "posted_lines_exist"}

        doomed = set(foreign.ids)

        def canonical(code):
            account = Account.search(
                [("company_ids", "=", company.id), ("code", "=", code)], limit=1
            )
            if not account:
                raise UserError(_("Kernel account %s is missing; publish the chart first.") % code)
            return account.id

        def points_at_doomed(record, field):
            return field in record._fields and record[field].id in doomed

        vals = {
            field: (canonical(code) if code else False)
            for field, code in self._FOREIGN_CHART_COMPANY_FIELDS.items()
            if points_at_doomed(company, field)
        }
        if vals:
            company.sudo().write(vals)

        for journal in env["account.journal"].sudo().search([("company_id", "=", company.id)]):
            jvals = {}
            if points_at_doomed(journal, "default_account_id"):
                jvals["default_account_id"] = canonical(
                    self._FOREIGN_CHART_JOURNAL_DEFAULT.get(journal.type, "60000")
                )
            for field, code in (("suspense_account_id", "10100"), ("profit_account_id", "40000"),
                                ("loss_account_id", "60000")):
                if points_at_doomed(journal, field):
                    jvals[field] = canonical(code)
            if jvals:
                journal.write(jvals)

        # Only template-created taxes go. Deleting every tax on the company
        # would be correct on a fresh tenant and wrong as a repair, where
        # setup_taxes may have built real US ones.
        self._template_artifacts(env, "account.tax", company).unlink()
        self._template_artifacts(env, "account.reconcile.model", company).unlink()
        # Groups last, since taxes reference them, and only those nothing points
        # at: _ensure_percent_tax reuses ANY existing group, so an operator tax
        # can be holding a template-created one.
        Tax = env["account.tax"].sudo()
        groups = self._template_artifacts(env, "account.tax.group", company)
        groups.filtered(lambda g: not Tax.search_count([("tax_group_id", "=", g.id)])).unlink()

        # Whatever group survived that is still allowed to point at a doomed
        # account, so it needs the same repointing as the rest of the plumbing.
        for group in env["account.tax.group"].sudo().search([("company_id", "=", company.id)]):
            gvals = {
                field: canonical("22000")
                for field in ("tax_payable_account_id", "tax_receivable_account_id")
                if points_at_doomed(group, field)
            }
            if gvals:
                group.write(gvals)

        # Guarded because the field only exists once stock accounting is
        # installed; searching it otherwise raises KeyError in the domain parser.
        if "account_stock_variation_id" in Account._fields:
            Account.search([("account_stock_variation_id", "in", list(doomed))]).write(
                {"account_stock_variation_id": False}
            )

        # Company-scoped default properties are checked on unlink, and the
        # fallback chart also leaves rows on OTHER companies pointing here.
        IrDefault = env["ir.default"].sudo()
        for default in IrDefault.search([]):
            raw = (default.json_value or "").strip('"')
            if not raw.isdigit() or int(raw) not in doomed:
                continue
            key = (default.field_id.model, default.field_id.name)
            target = self._FOREIGN_CHART_DEFAULTS.get(key)
            if target and default.company_id.id == company.id:
                IrDefault.set(key[0], key[1], canonical(target), company_id=company.id)
            else:
                default.unlink()

        count = len(foreign)
        foreign.unlink()
        _logger.info("Purged %s foreign chart accounts from company %s.", count, company.id)
        return {"deleted": count}

    @api.model
    def create_owned_company(self, name):
        """Create a company for the authenticated caller and grant them access.

        When ``self.env.context.get('defer_company_access')`` is True the
        ``company_ids`` write on the session user is skipped entirely.  This
        lets callers (e.g. ``setup_entities``) create several companies in one
        shot and then issue a single batched write via
        ``_batch_grant_company_access`` — avoiding N session-cookie
        invalidations that would log the operator out mid-onboarding.
        """
        if not self.env.user.has_group("base.group_user"):
            raise AccessError(_("Only internal users can create a company."))
        allowed = (
            self.env.user.has_group("meridian_saas.group_meridian_saas_manager")
            or self.env.user.has_group("meridian_saas.group_meridian_saas_accountant")
            or self.env.user.has_group("account.group_account_manager")
        )
        if not allowed:
            raise AccessError(_("Only workspace managers or accountants can create a company."))

        name = (name or "").strip()
        if not name:
            return {"status": "invalid", "message": "Company name is required."}

        user = self.env.user
        if self.env["res.company"].sudo().search_count([("name", "=", name)]):
            return {"status": "duplicate", "message": "That company name is already taken. Choose another."}

        required_modules = ["account", "l10n_us", "l10n_us_account", "poseidon_accounting_kernel"]
        missing = []
        for mod_name in required_modules:
            mod = self.env["ir.module.module"].sudo().search([("name", "=", mod_name)], limit=1)
            if not mod or mod.state != "installed":
                missing.append(mod_name)
        if missing:
            return {"status": "blocked", "message": f"Missing required modules: {', '.join(missing)}"}

        currency_usd = self.env.ref("base.USD", raise_if_not_found=False)
        try:
            with self.env.cr.savepoint():
                company = self.env["res.company"].sudo().create({
                    "name": name,
                    "currency_id": currency_usd.id if currency_usd else False,
                })
        except IntegrityError:
            if self.env["res.company"].sudo().search_count([("name", "=", name)]):
                return {"status": "duplicate", "message": "That company name is already taken. Choose another."}
            raise
        self._configure_company(company)

        # Only grant immediate access when not in bulk-creation mode.  Callers
        # that set 'defer_company_access' are responsible for calling
        # _batch_grant_company_access once all companies are ready.
        defer = self.env.context.get("defer_company_access", False)
        if not defer:
            if company.id not in user.company_ids.ids:
                user.sudo().write({"company_ids": [(4, company.id)]})
            if not user.company_id:
                user.sudo().write({"company_id": company.id})

        group_manager = self.env.ref(
            "meridian_saas.group_meridian_saas_manager", raise_if_not_found=False
        )
        if group_manager and user.id not in group_manager.user_ids.ids:
            group_manager.sudo().write({"user_ids": [(4, user.id)]})

        return {"status": "completed", "company_id": company.id, "name": company.name}

    @api.model
    def _batch_grant_company_access(self, user, company_ids):
        """Grant the user access to several companies in a single DB write.

        Issuing one ``company_ids`` write instead of N writes avoids repeated
        Odoo session-cookie invalidations that would otherwise log the operator
        out during multi-entity onboarding.  Only companies not already in
        ``user.company_ids`` are included in the write.
        """
        new_ids = [cid for cid in company_ids if cid not in user.company_ids.ids]
        if not new_ids:
            return
        user.sudo().write({"company_ids": [(4, cid) for cid in new_ids]})

    def _log_event(self, request_id, event_kind, message):
        self.env["meridian.saas.event"].sudo().create({
            "request_id": request_id,
            "event_kind": event_kind,
            "message": message,
        })
        _logger.info("SaaS Webhook Event [%s] for Request ID %s: %s", event_kind, request_id, message)
