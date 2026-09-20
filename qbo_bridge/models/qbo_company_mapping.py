import logging
from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

ENTITY_TYPES = [
    ("account", "Chart of Accounts"),
    ("partner", "Customers & Vendors"),
    ("invoice", "Invoices & Bills"),
    ("payment", "Payments & Transactions"),
    ("journal_entry", "Journal Entries"),
    ("product", "Products / Items"),
]

# Only the states something in this codebase actually sets or gates on. The
# ticket's full vocabulary (dry_run_passed, mappings_reviewed, sync_failed, …)
# lands with the phase that writes it — adding a Selection value costs nothing
# later, and an unreachable state is just a lie in a dropdown.
ONBOARDING_STATES = [
    ("not_started", "Not started"),
    ("qbo_connected", "QuickBooks connected"),
    ("readiness_checked", "Readiness checked"),
    ("setup_prepared", "Environment prepared"),
    ("initial_sync_running", "Initial sync running"),
    ("active", "Active"),
    ("blocked", "Blocked"),
    ("reauthorization_required", "Reauthorization required"),
]

# The four transitions the product contract forbids outright (ClickUp 86bb6vcpf
# §3). Everything else is allowed: onboarding is re-entrant by design, and a
# whitelist would make every future step a migration.
FORBIDDEN_TRANSITIONS = frozenset({
    ("not_started", "initial_sync_running"),
    ("qbo_connected", "active"),
    ("blocked", "initial_sync_running"),
    ("reauthorization_required", "initial_sync_running"),
})

# Readiness may advance the state only out of these — never out of a later one,
# so re-running the diagnosis on a live mapping does not rewind it.
_PRE_DIAGNOSIS_STATES = ("not_started", "qbo_connected")


class QboCompanyMapping(models.Model):
    """Links one Odoo company to one QBO realm for synchronisation.

    Because some umbrella companies share a single QBO realm, this model
    supports the mixed layout: multiple Odoo companies can reference the
    same qbo.realm record. Each mapping carries its own entity toggles,
    sync schedule, and last-sync timestamp.
    """

    _name = "qbo.company.mapping"
    _description = "Odoo company ↔ QBO realm mapping"
    _order = "company_id, realm_id"
    _rec_name = "display_name"

    # ── Core link ─────────────────────────────────────────────────────────────
    company_id = fields.Many2one(
        "res.company",
        string="Odoo company",
        required=True,
        ondelete="cascade",
    )
    realm_id = fields.Many2one(
        "qbo.realm",
        string="QBO realm",
        required=True,
        ondelete="restrict",
    )

    display_name = fields.Char(compute="_compute_display_name", store=True)

    @api.depends("company_id", "realm_id")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = f"{rec.company_id.name} → {rec.realm_id.name}"

    # ── Sync toggles ──────────────────────────────────────────────────────────
    sync_enabled = fields.Boolean(string="Sync enabled", default=True)
    sync_accounts = fields.Boolean(string="Chart of Accounts", default=True)
    sync_partners = fields.Boolean(string="Customers & Vendors", default=True)
    sync_invoices = fields.Boolean(string="Invoices & Bills", default=True)
    sync_payments = fields.Boolean(string="Payments & Transactions", default=True)
    sync_journal_entries = fields.Boolean(string="Journal Entries", default=False)
    sync_products = fields.Boolean(string="Products / Items", default=True)
    historical_backfill = fields.Boolean(
        string="Import full history",
        default=False,
        copy=False,
        help="On the next pull, ignore last-sync timestamps and import the full "
        "QuickBooks history instead of only changes since the last sync.",
    )

    # ── Timestamps ────────────────────────────────────────────────────────────
    last_sync_date = fields.Datetime(string="Last full sync", readonly=True)
    last_sync_accounts = fields.Datetime(string="Last accounts sync", readonly=True)
    last_sync_partners = fields.Datetime(string="Last partners sync", readonly=True)
    last_sync_invoices = fields.Datetime(string="Last invoices sync", readonly=True)
    last_sync_payments = fields.Datetime(string="Last payments sync", readonly=True)
    last_sync_journal_entries = fields.Datetime(string="Last JE sync", readonly=True)
    last_sync_products = fields.Datetime(string="Last products sync", readonly=True)

    # ── Schedule ──────────────────────────────────────────────────────────────
    sync_interval_minutes = fields.Integer(
        string="Sync interval (min)",
        default=60,
        help="Minimum minutes between automatic syncs. 0 = cron-only.",
    )
    sync_requested = fields.Boolean(
        string="Manual sync requested",
        default=False,
        copy=False,
        help="Set by a manual trigger. The sync cron processes flagged mappings "
        "immediately, ignoring the interval throttle, then clears the flag.",
    )

    # ── Onboarding workflow ───────────────────────────────────────────────────
    onboarding_state = fields.Selection(
        ONBOARDING_STATES,
        string="Onboarding state",
        default="not_started",
        copy=False,
        index=True,
        help="Where this mapping sits in the QuickBooks onboarding contract.",
    )
    last_readiness_at = fields.Datetime(string="Last readiness check", readonly=True)
    configuration_snapshot = fields.Json(
        string="Latest QBO configuration",
        readonly=True,
        copy=False,
    )
    configuration_synced_at = fields.Datetime(
        string="Configuration received at",
        readonly=True,
        copy=False,
    )

    # ── Stats ─────────────────────────────────────────────────────────────────
    conflict_count = fields.Integer(
        compute="_compute_conflict_count", string="Open conflicts",
    )
    log_count = fields.Integer(compute="_compute_log_count", string="Sync logs")

    def _compute_conflict_count(self):
        for rec in self:
            rec.conflict_count = self.env["qbo.conflict"].search_count(
                [("mapping_id", "=", rec.id), ("status", "=", "pending")],
            )

    def _compute_log_count(self):
        for rec in self:
            rec.log_count = self.env["qbo.sync.log"].search_count(
                [("mapping_id", "=", rec.id)],
            )

    def write(self, vals):
        target = vals.get("onboarding_state")
        if target:
            for record in self:
                if (record.onboarding_state, target) in FORBIDDEN_TRANSITIONS:
                    raise UserError(
                        _("Cannot move this QuickBooks mapping from %(source)s to %(target)s.")
                        % {"source": record.onboarding_state, "target": target},
                    )
        return super().write(vals)

    # ── SQL constraint ─────────────────────────────────────────────────────────
    _unique_company_realm = models.Constraint(
        "UNIQUE(company_id, realm_id)",
        "An Odoo company can only have one mapping per QBO realm.",
    )

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_request_pull(self):
        """Queue an asynchronous pull-only sync (QBO → Odoo) and return at once.

        This is the explicit, honest entry point for the BFF. The MVP is hard
        pull-only: the queued work reads from QuickBooks and writes into Odoo
        only — it never writes back to QuickBooks.

        A full pull can run for minutes (large general ledgers), far longer than
        the HTTP/RPC budget of a manual trigger from the BFF. Instead of running
        it inline, flag the mapping and ask the sync cron to run ASAP in a worker
        process. The caller gets a fast acknowledgement and watches the sync log
        for progress.
        """
        self.ensure_one()
        self._ensure_pull_ready()
        self.write({"sync_requested": True})

        # Leave a queue marker in the audit log for each entity that will be
        # pulled, so the BFF and operators can see the request was accepted
        # before the worker runs.
        for entity_type, enabled in self._enabled_pull_entities():
            if enabled:
                self.env["qbo.sync.log"].log(
                    self.env, self, entity_type, "pull", "success", "queue",
                    message="Pull-only sync queued (action_request_pull)",
                )

        cron = self.env.ref("qbo_bridge.ir_cron_qbo_sync", raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()
        return {"queued": True, "mode": "pull", "mapping_id": self.id}

    def action_request_sync(self):
        """Backwards-compatible alias for :meth:`action_request_pull`.

        Retained so existing callers keep working. The MVP performs a pull-only
        sync, so this simply delegates to the honestly-named pull action.
        """
        return self.action_request_pull()

    def action_request_historical_backfill(self):
        """Queue an async full-history pull (QBO → Odoo) and return at once.

        Same queue as :meth:`action_request_pull`, but the worker ignores the
        last-sync timestamps on the next run and imports the full QuickBooks
        history. The worker clears the flag after the pull completes.
        """
        self.ensure_one()
        self._ensure_pull_ready()
        self.write({"historical_backfill": True})
        return self.action_request_pull()

    def _enabled_pull_entities(self):
        """Return (entity_type, enabled) pairs in the order sync_all pulls them."""
        self.ensure_one()
        return [
            ("account", self.sync_accounts),
            ("partner", self.sync_partners),
            ("product", self.sync_products),
            ("invoice", self.sync_invoices),
            ("payment", self.sync_payments),
            ("journal_entry", self.sync_journal_entries),
        ]

    def action_sync_now(self):
        """Run an immediate full pull (QBO → Odoo) for this mapping, inline.

        The MVP is pull-only: this reads from QuickBooks into Odoo and never
        writes back. Used by the cron worker and tests. Do not call this from a
        request-bound path (BFF/controller) — use action_request_pull() so the
        work runs async.
        """
        self.ensure_one()
        self._ensure_pull_ready()
        from ..services.qbo_sync_engine import QBOSyncEngine  # noqa: PLC0415

        engine = QBOSyncEngine(self.env, self)
        engine.sync_all()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Pull complete"),
                "message": _("Pulled the latest QuickBooks data into %s") % self.display_name,
                "type": "success",
            },
        }

    def _ensure_pull_ready(self):
        """Refuse a pull that readiness says cannot succeed.

        The connection checks stay first so a disconnected realm still fails
        with the message operators already recognise; everything else comes from
        the readiness diagnosis, itemised.
        """
        self.ensure_one()
        realm = self.realm_id.sudo()
        if realm.sync_mode != "pull_only" or realm.state != "connected" or not realm.refresh_token:
            raise UserError(_("Connect or re-authorise QuickBooks before starting live sync."))
        from ..services.qbo_readiness import blocking  # noqa: PLC0415

        readiness = self.get_sync_readiness()
        if readiness["state"] == "blocked":
            raise UserError(
                _("QuickBooks sync is blocked:\n%s")
                % "\n".join(f"- {check['message']}" for check in blocking(readiness)),
            )

    def get_sync_readiness(self):
        """Diagnose everything that would stop this mapping from pulling cleanly.

        Stamps when the diagnosis last ran. That is metadata, not an accounting
        mutation: it emits no audit reference and invalidates no cache, so the
        BFF may call it from a read route.
        """
        self.ensure_one()
        from ..services.qbo_readiness import compute_readiness  # noqa: PLC0415

        result = compute_readiness(self.env, self)
        vals = {"last_readiness_at": fields.Datetime.now()}
        if self.onboarding_state in _PRE_DIAGNOSIS_STATES:
            vals["onboarding_state"] = "readiness_checked"
        self.sudo().write(vals)
        return result

    def preview_sync_prerequisites(self):
        """List what "Prepare environment" would create. Never writes."""
        self.ensure_one()
        from ..services.qbo_prepare import plan_prerequisites  # noqa: PLC0415

        return {
            "preview_only": True,
            "mapping_id": self.id,
            "planned": plan_prerequisites(self.env, self),
            "readiness": self.get_sync_readiness(),
        }

    def prepare_sync_prerequisites(self, human_confirmed=False):
        """Create the safe, missing prerequisites for a pull.

        Journals only — the kernel owns the chart of accounts. Refuses without
        an explicit confirmation so the BFF gate cannot be bypassed by an RPC
        caller.
        """
        self.ensure_one()
        if human_confirmed is not True:
            raise UserError(
                _("Confirm the prepare preview before changing this company's setup."),
            )
        from ..services.qbo_prepare import apply_prerequisites, summarise  # noqa: PLC0415

        created = apply_prerequisites(self.env, self)
        log = summarise(self.env, self, created)
        readiness = self.get_sync_readiness()
        if readiness["state"] != "blocked":
            self.sudo().write({"onboarding_state": "setup_prepared"})
        return {
            "created": created,
            "audit_model": "qbo.sync.log",
            "audit_ref": str(log.id),
            "readiness": readiness,
        }

    def action_view_conflicts(self):
        return {
            "type": "ir.actions.act_window",
            "name": _("Conflicts – %s") % self.display_name,
            "res_model": "qbo.conflict",
            "view_mode": "list,form",
            "domain": [("mapping_id", "=", self.id), ("status", "=", "pending")],
        }

    def action_view_logs(self):
        return {
            "type": "ir.actions.act_window",
            "name": _("Sync log – %s") % self.display_name,
            "res_model": "qbo.sync.log",
            "view_mode": "list",
            "domain": [("mapping_id", "=", self.id)],
        }

    # ── Timestamp helper used by the sync engine ──────────────────────────────

    def get_last_sync_for(self, entity_type):
        """Return the datetime of the last successful sync for a given entity type."""
        field_map = {
            "account": "last_sync_accounts",
            "partner": "last_sync_partners",
            "invoice": "last_sync_invoices",
            "payment": "last_sync_payments",
            "journal_entry": "last_sync_journal_entries",
            "product": "last_sync_products",
        }
        fname = field_map.get(entity_type)
        return getattr(self, fname, False) if fname else False

    def set_last_sync_for(self, entity_type):
        """Stamp the last sync time for a given entity type."""
        field_map = {
            "account": "last_sync_accounts",
            "partner": "last_sync_partners",
            "invoice": "last_sync_invoices",
            "payment": "last_sync_payments",
            "journal_entry": "last_sync_journal_entries",
            "product": "last_sync_products",
        }
        fname = field_map.get(entity_type)
        if fname:
            self.write({fname: fields.Datetime.now(), "last_sync_date": fields.Datetime.now()})
