from odoo import _, api, models
from odoo.exceptions import UserError


class AccountChartTemplate(models.AbstractModel):
    _inherit = "account.chart.template"

    @api.model
    def poseidon_activate_standard_account_for_company(
        self, company_id, standard_account_id=False, code=False
    ):
        company = self.env["res.company"].browse(company_id)
        if not company:
            raise UserError(_("Company not found."))

        # Master-chart rows are read-only canonical data. Meridian's MCP role
        # guard authorizes the operation; it must not require QBO UI ACLs just
        # to resolve the approved master codes.
        StandardAccount = self.env["qbo.standard.account"].sudo()
        standard_account = False
        if standard_account_id:
            standard_account = StandardAccount.browse(standard_account_id)
        elif code:
            standard_account = StandardAccount.search(
                [("code", "=", code), ("entry_type", "=", "detail")],
                limit=1,
            )

        if not standard_account:
            raise UserError(_("Master account not found."))

        Account = self.env["account.account"].with_company(company)
        account = Account.search(
            [
                ("company_ids", "=", company.id),
                ("qbo_standard_account_id", "=", standard_account.id),
            ],
            limit=1,
        )

        if account and any(
            field in account._fields and account[field]
            for field in ("qbo_id", "qbo_source_name")
        ):
            raise UserError(
                _(
                    "This account is a historical QBO source. Activate a distinct Kernel destination instead of transforming the source."
                )
            )

        action = "already_present"
        if not account:
            account = Account.search(
                [
                    ("company_ids", "=", company.id),
                    ("code", "=", standard_account.code),
                ],
                limit=1,
            )

            if account:
                if any(
                    field in account._fields and account[field]
                    for field in ("qbo_id", "qbo_source_name")
                ):
                    raise UserError(
                        _(
                            "This code belongs to a historical QBO source. Choose a distinct Kernel destination code."
                        )
                    )
                account.write({"qbo_standard_account_id": standard_account.id})
                action = "linked"
            else:
                vals = standard_account.prepare_company_account_vals(company)
                account = Account.create(vals)
                action = "created"

        # L3 kernel accounts roll up into their L0/L1 parent: wire the link so
        # the account nests under the right node in the visual chart. Idempotent
        # and a no-op for L0/L1 accounts (no rollup target) or locked ones.
        if account:
            self._ensure_l3_parent_link(account, standard_account, company)

        return {
            "action": action,
            "account": {
                "id": account.id,
                "standard_id": standard_account.id,
                "code": account.code,
                "name": account.name,
                "internal_group": account.internal_group or account.account_type,
                "locked": account.poseidon_kernel_locked
                if "poseidon_kernel_locked" in account._fields
                else False,
                # Field-guarded like poseidon_kernel_locked above: the rollup
                # parent lives in poseidon_accounting_kernel, which this module
                # does not depend on. Reading it unguarded crashed activation on
                # any install without the kernel module.
                "parent_code": (
                    account.poseidon_parent_account_id.code
                    if "poseidon_parent_account_id" in account._fields
                    and account.poseidon_parent_account_id
                    else False
                ),
            },
        }

    @api.model
    def poseidon_publish_missing_standard_accounts(
        self, company_id, update_existing=True, required_only=False
    ):
        company = self.env["res.company"].browse(company_id)
        if not company:
            raise UserError(_("Company not found."))

        StandardAccount = self.env["qbo.standard.account"]
        StandardAccount._ensure_master_chart_imported()
        stats = StandardAccount.sync_detail_accounts_to_company(
            company,
            update_existing=update_existing,
            required_only=required_only,
        )
        return {
            "company_id": company.id,
            "company": company.name,
            "stats": stats,
        }

    # ------------------------------------------------------------------
    # L3 analytic kernel activation (on demand, reversible, never bulk)
    # ------------------------------------------------------------------

    @api.model
    def _l3_standards_by_codes(self, codes):
        StandardAccount = self.env["qbo.standard.account"].sudo()
        StandardAccount._ensure_l3_master_chart_imported()
        return StandardAccount.search(
            [
                ("code", "in", list(codes) or ["__none__"]),
                ("entry_type", "=", "detail"),
                ("kernel_layer", "=", "L3"),
                ("active", "=", True),
            ],
            order="code",
        )

    @api.model
    def _ensure_l3_parent_link(self, account, standard, company):
        """Link an activated L3 account to its L0/L1 parent (rollup target).

        Field-guarded: only when poseidon_accounting_kernel is installed and the
        account is not locked. A missing parent in the company is not an error —
        the L3 account stays independently usable and the rollup link is skipped.
        """
        if "poseidon_parent_account_id" not in account._fields:
            return False
        if account.poseidon_parent_account_id:
            return False
        if (
            "poseidon_kernel_locked" in account._fields
            and account.poseidon_kernel_locked
        ):
            return False
        parent_code = (standard.rollup_to_l1_parent or "").strip()
        if not parent_code:
            return False
        Account = (
            self.env["account.account"]
            .with_company(company)
            .with_context(active_test=False)
        )
        parent = Account.search(
            [
                ("company_ids", "=", company.id),
                ("code", "=", parent_code),
            ],
            limit=1,
        )
        if not parent or parent.id == account.id:
            return False
        account.write({"poseidon_parent_account_id": parent.id})
        return True

    @api.model
    def poseidon_activate_l3_accounts(self, company_id, codes):
        """Activate specific L3 analytic accounts for a company.

        Creates the company account for each L3 master account (or links the
        existing one) and wires poseidon_parent_account_id to its L0/L1 rollup
        parent. Individual, reversible, and never affects other companies.
        """
        company = self.env["res.company"].browse(company_id)
        if not company:
            raise UserError(_("Company not found."))
        if isinstance(codes, str):
            codes = [part.strip() for part in codes.split(",") if part.strip()]
        codes = [str(code).strip() for code in (codes or []) if str(code).strip()]
        if not codes:
            raise UserError(_("No L3 account codes provided."))

        standards = self._l3_standards_by_codes(codes)
        found = {standard.code for standard in standards}
        missing = sorted(set(codes) - found)
        if missing:
            raise UserError(
                _(
                    "L3 account codes not found in the master chart: %(codes)s. "
                    "L3 accounts must exist in the kernel before activation.",
                )
                % {"codes": ", ".join(missing)},
            )

        results = []
        for standard in standards:
            outcome = self.poseidon_activate_standard_account_for_company(
                company_id,
                standard_account_id=standard.id,
            )
            account = self.env["account.account"].browse(outcome["account"]["id"])
            outcome["account"]["parent_code"] = False
            if self._ensure_l3_parent_link(account, standard, company):
                outcome["account"]["parent_code"] = (
                    account.poseidon_parent_account_id.code
                )
            outcome["account"]["activity_tag"] = standard.activity_tag or False
            outcome["account"]["functional_group"] = standard.functional_group or False
            results.append(outcome)

        return {
            "company_id": company.id,
            "company": company.name,
            "activated": len(results),
            "accounts": results,
        }

    @api.model
    def _l3_activity_tags_for_company(self, company):
        """Derive the L3 activity tags for a company from its US tax profile.

        Convenience preselection only — the profile activity_tag maps onto L3
        activity tags (services->Services, trade_retail->Trade/Retail,
        manufacturing->Manufacturing, everything else->Universal). Never a gate.
        """
        if "poseidon.us.tax.profile" not in self.env:
            return []
        profile = (
            self.env["poseidon.us.tax.profile"]
            .sudo()
            .search(
                [("company_id", "=", company.id), ("active", "=", True)],
                limit=1,
            )
        )
        if not profile or not hasattr(profile, "poseidon_l3_activity_tags"):
            return []
        return profile.poseidon_l3_activity_tags()

    @api.model
    def _l3_batch_codes(self, company, batch_type, batch_key):
        StandardAccount = self.env["qbo.standard.account"].sudo()
        StandardAccount._ensure_l3_master_chart_imported()
        domain = [
            ("entry_type", "=", "detail"),
            ("kernel_layer", "=", "L3"),
            ("active", "=", True),
        ]
        if batch_type == "activity_tag":
            tags = batch_key or self._l3_activity_tags_for_company(company)
            if not tags:
                return []
            if isinstance(tags, str):
                tags = [tags]
            # Compound tags exist in the artifact (e.g. "Trade/Retail/Manufacturing"
            # or "Services/Manufacturing"): an exact match would miss them, so use
            # a token-wise OR ilike match against the canonical tag names.
            tag_domain = [("activity_tag", "ilike", tag) for tag in tags]
            standards = StandardAccount.search(
                domain + ["|"] * (len(tag_domain) - 1) + tag_domain,
                order="code",
            )
            return standards.mapped("code")

        if batch_type == "functional_group":
            batch_key = (batch_key or "").strip()
            if not batch_key:
                raise UserError(_("A functional_group batch requires a batch_key."))
            standards = StandardAccount.search(
                domain + [("functional_group", "=", batch_key)],
                order="code",
            )
            return standards.mapped("code")

        if batch_type == "dashboard_account_set":
            batch_key = (batch_key or "").strip()
            if not batch_key:
                raise UserError(
                    _("A dashboard_account_set batch requires a batch_key.")
                )
            standards = StandardAccount.search(
                domain + [("dashboard_account_sets", "!=", False)],
                order="code",
            )
            return [
                standard.code
                for standard in standards
                if batch_key in (standard.dashboard_account_sets or "").split(",")
            ]

        raise UserError(
            _(
                "Unknown L3 batch type '%(batch)s'. Use activity_tag, "
                "functional_group or dashboard_account_set.",
            )
            % {"batch": batch_type},
        )

    @api.model
    def poseidon_activate_l3_batch(
        self, company_id, batch_type, batch_key=False, preview=False
    ):
        """Activate an L3 batch for a company, preselected by tag / group / set.

        batch_type: activity_tag | functional_group | dashboard_account_set.
        When batch_key is omitted for activity_tag, the company's US tax profile
        activity_tag drives the preselection. preview=True returns the selected
        codes without creating anything.
        """
        company = self.env["res.company"].browse(company_id)
        if not company:
            raise UserError(_("Company not found."))
        batch_type = (batch_type or "").strip().lower()
        if batch_type not in (
            "activity_tag",
            "functional_group",
            "dashboard_account_set",
        ):
            raise UserError(
                _(
                    "Unknown L3 batch type '%(batch)s'. Use activity_tag, "
                    "functional_group or dashboard_account_set.",
                )
                % {"batch": batch_type},
            )

        codes = self._l3_batch_codes(company, batch_type, batch_key)
        if not codes:
            return {
                "company_id": company.id,
                "preview": bool(preview),
                "batch_type": batch_type,
                "batch_key": batch_key or None,
                "count": 0,
                "codes": [],
                "accounts": [],
            }

        if preview:
            return {
                "company_id": company.id,
                "preview": True,
                "batch_type": batch_type,
                "batch_key": batch_key or None,
                "count": len(codes),
                "codes": list(codes),
                "accounts": [],
            }

        result = self.poseidon_activate_l3_accounts(company_id, codes)
        result["preview"] = False
        result["batch_type"] = batch_type
        result["batch_key"] = batch_key or None
        return result

    @api.model
    def poseidon_deactivate_l3_accounts(self, company_id, codes):
        """Reversibly deactivate L3 analytic accounts for a company.

        Only affects company accounts linked to L3 master accounts. Locked
        accounts or accounts with journal items are reported as blocked, never
        silently touched; reactivation simply re-runs the activation.
        """
        company = self.env["res.company"].browse(company_id)
        if not company:
            raise UserError(_("Company not found."))
        if isinstance(codes, str):
            codes = [part.strip() for part in codes.split(",") if part.strip()]
        codes = [str(code).strip() for code in (codes or []) if str(code).strip()]
        if not codes:
            raise UserError(_("No L3 account codes provided."))

        Account = (
            self.env["account.account"]
            .with_company(company)
            .with_context(active_test=False)
        )
        accounts = Account.search(
            [
                ("company_ids", "=", company.id),
                ("code", "in", codes),
                ("qbo_standard_account_id.kernel_layer", "=", "L3"),
            ],
            order="code",
        )
        deactivated = []
        blocked = []
        skipped = []
        for account in accounts:
            if not account.active:
                skipped.append(account.code)
                continue
            if (
                "poseidon_kernel_locked" in account._fields
                and account.poseidon_kernel_locked
            ):
                blocked.append(account.code)
                continue
            if (
                hasattr(account, "_poseidon_has_move_lines")
                and account._poseidon_has_move_lines()
            ):
                blocked.append(account.code)
                continue
            try:
                account.active = False
                deactivated.append(account.code)
            except Exception:
                blocked.append(account.code)

        return {
            "company_id": company.id,
            "company": company.name,
            "deactivated": deactivated,
            "blocked": blocked,
            "skipped": skipped,
            "deactivated_count": len(deactivated),
            "blocked_count": len(blocked),
            "skipped_count": len(skipped),
        }

    @api.model
    def poseidon_unpublish_l3(self, company_id):
        """Deactivate every L3 analytic account published to a company."""
        company = self.env["res.company"].browse(company_id)
        if not company:
            raise UserError(_("Company not found."))
        Account = (
            self.env["account.account"]
            .with_company(company)
            .with_context(active_test=False)
        )
        accounts = Account.search(
            [
                ("company_ids", "=", company.id),
                ("qbo_standard_account_id.kernel_layer", "=", "L3"),
            ],
        )
        codes = accounts.mapped("code")
        result = self.poseidon_deactivate_l3_accounts(company_id, codes)
        result["company"] = company.name
        return result
