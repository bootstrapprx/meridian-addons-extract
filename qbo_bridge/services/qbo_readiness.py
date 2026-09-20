"""Readiness diagnosis for one qbo.company.mapping (ClickUp 86bb6vcpf).

Answers one question before any pull runs: what would stop this mapping from
importing cleanly, and who has to fix it? Read-only — it never writes an
accounting record.

Severity vocabulary
-------------------
block  — a real pull cannot run, or would import garbage
review — a human decision is pending
warn   — the pull runs, with a limitation the operator must know about

Fix hints tell the UI where to send the operator:
auto    — "Prepare environment" can fix it (see qbo_prepare.py)
connect — the QuickBooks connection needs attention
chart   — the kernel chart has to be published to this company
configuration — review QBO-derived company and bank suggestions
mapping — materialise and review account-mapping suggestions
manual  — a human has to go and do something specific
"""
from odoo import _, fields

BLOCK = "block"
REVIEW = "review"
WARN = "warn"

# How complete the pull engine is per entity, as of services/qbo_sync_engine.py.
#   full      — upserts real Odoo records
#   partial   — creates records with known gaps (see the message)
#   file_only — only imported from an uploaded package, never from the API
#   none      — the upsert is a stub that logs an honest skip
ENGINE_SUPPORT = {
    "account": "full",
    "partner": "full",
    "product": "full",
    "invoice": "partial",
    "payment": "partial",
    "journal_entry": "partial",
}

ENGINE_GAP_MESSAGE = {
    "partial": (
        "%(label)s import preserves partners, lines and optional QBO Classes, but "
        "does not infer taxes or post documents. Review every imported draft."
    ),
    "file_only": (
        "%(label)s is only imported from an uploaded QuickBooks package, not from "
        "the live API. Live pulls will record skips."
    ),
    "none": (
        "%(label)s has no importer yet. Records are logged as auditable skips, "
        "not imported."
    ),
}

# Journal types "Prepare environment" is allowed to create, keyed by the
# readiness check that reports each one missing. Kept here so readiness and
# prepare can never disagree about what "auto" means.
PREPARABLE_JOURNALS = (
    # (check key, journal type, default name, code prefix, gating toggle)
    ("journal_sale", "sale", "Sales", "SAL", "sync_invoices"),
    ("journal_purchase", "purchase", "Vendor Bills", "BIL", "sync_invoices"),
    ("journal_general", "general", "General", "GEN", "sync_journal_entries"),
)


def compute_readiness(env, mapping):
    """Return the readiness snapshot for one mapping. Never writes."""
    checks = []

    def add(key, severity, message, fix=None):
        checks.append(
            {"key": key, "severity": severity, "message": message, "fix": fix},
        )

    company = mapping.company_id
    realm = mapping.realm_id.sudo()

    _check_connection(add, company, realm)
    _check_accounting_substrate(env, add, company)
    _check_journals(env, add, mapping, company)
    _check_bank_journals(env, add, mapping, company)
    _check_engine_support(add, mapping)
    _check_open_decisions(env, add, mapping, company)

    severities = {check["severity"] for check in checks}
    if BLOCK in severities:
        state = "blocked"
    elif REVIEW in severities:
        state = "needs_review"
    elif WARN in severities:
        state = "ready_with_warnings"
    else:
        state = "ready_for_sync"

    return {
        "state": state,
        "mapping_id": mapping.id,
        "company_id": company.id,
        "company_name": company.name,
        "realm_id": realm.realm_id,
        "checked_at": fields.Datetime.to_string(fields.Datetime.now()),
        "checks": checks,
        "auto_actions": sorted({c["key"] for c in checks if c["fix"] == "auto"}),
    }


def blocking(result):
    """The checks that stop a pull. Callers filter — the payload ships one list."""
    return [check for check in result["checks"] if check["severity"] == BLOCK]


def _check_connection(add, company, realm):
    if not company.active:
        add("company_active", BLOCK, _("The Odoo company is archived."), "manual")
    if not company.currency_id.active:
        add(
            "currency_active",
            BLOCK,
            _("The company currency %s is archived.") % company.currency_id.name,
            "manual",
        )
    if realm.state != "connected":
        add("realm_connected", BLOCK, _("QuickBooks is not connected."), "connect")
    if not realm.refresh_token:
        add(
            "refresh_token",
            BLOCK,
            _("No QuickBooks refresh token — reauthorise the connection."),
            "connect",
        )
    if realm.sync_mode != "pull_only":
        add(
            "sync_mode",
            BLOCK,
            _("This realm is in file-upload mode; live pull is disabled."),
            "connect",
        )
    if realm.refresh_token and not realm.token_valid:
        add(
            "token_expiry",
            WARN,
            _("The QuickBooks access token has expired; the next pull refreshes it."),
        )

    # Umbrella groups deliberately share one realm across companies, so this is a
    # warning, not the block the ticket originally proposed — see the pre-flight.
    siblings = realm.mapping_ids.filtered(lambda m: m.company_id != company)
    if siblings:
        add(
            "realm_shared",
            WARN,
            _("This QuickBooks realm is also mapped to: %s.")
            % ", ".join(siblings.mapped("company_id.name")),
        )


def _check_accounting_substrate(env, add, company):
    Account = env["account.account"].sudo()
    base = [("company_ids", "=", company.id)]

    if not Account.search_count(base + [("account_type", "=", "asset_receivable")]):
        add(
            "account_receivable",
            BLOCK,
            _("No receivable account on this company — invoices cannot be posted."),
            "chart",
        )
    if not Account.search_count(base + [("account_type", "=", "liability_payable")]):
        add(
            "account_payable",
            BLOCK,
            _("No payable account on this company — vendor bills cannot be posted."),
            "chart",
        )

    # Soft probes: these models/fields belong to modules that depend on
    # qbo_bridge, so they may legitimately be absent.
    if "qbo_standard_account_id" in Account._fields:
        published = Account.search_count(base + [("qbo_standard_account_id", "!=", False)])
        if not published:
            add(
                "chart_published",
                BLOCK,
                _("The kernel chart of accounts has not been published to this company."),
                "chart",
            )
    if "poseidon.kernel.version" in env:
        kernel = env["poseidon.kernel.version"].sudo().get_installed_kernel_status()
        if not kernel.get("installed"):
            add(
                "kernel_installed",
                BLOCK,
                _("No Poseidon accounting kernel is installed on this database."),
                "manual",
            )
        for warning in kernel.get("warnings") or []:
            add("kernel_warning", WARN, warning)


def _check_journals(env, add, mapping, company):
    present = set(
        env["account.journal"]
        .sudo()
        .search([("company_id", "=", company.id)])
        .mapped("type"),
    )
    messages = {
        "sale": _("No sales journal — customer invoices cannot be imported."),
        "purchase": _("No purchase journal — vendor bills cannot be imported."),
        "general": _("No general journal — journal entries cannot be imported."),
    }
    for key, journal_type, _name, _prefix, toggle in PREPARABLE_JOURNALS:
        if journal_type in present:
            continue
        severity = BLOCK if getattr(mapping, toggle) else WARN
        add(key, severity, messages[journal_type], "auto")


def qbo_bank_accounts(env, company):
    Account = env["account.account"].sudo()
    if "qbo_source_account_type" not in Account._fields:
        return Account.browse()
    sources = Account.search(
        [
            ("company_ids", "=", company.id),
            ("qbo_source_account_type", "=", "Bank"),
            ("active", "=", True),
        ],
    )
    account_ids = []
    for source in sources:
        account = qbo_operational_account(env, company, source)
        if account and account.id not in account_ids:
            account_ids.append(account.id)
    return Account.browse(account_ids)


def qbo_operational_account(env, company, account):
    """Use a confirmed canonical destination when governance treated QBO data."""
    if not account or not account.qbo_id or "poseidon.mapping.decision" not in env:
        return account
    decision = env["poseidon.mapping.decision"].search(
        [
            ("company_id", "=", company.id),
            ("qbo_id", "=", account.qbo_id),
        ],
        limit=1,
    )
    if not decision:
        return account
    if decision.state == "confirmed" and decision.destination_account_id.active:
        return decision.destination_account_id
    return env["account.account"].browse()


def _check_bank_journals(env, add, mapping, company):
    bank_accounts = qbo_bank_accounts(env, company)
    bank_journals = env["account.journal"].sudo().search(
        [("company_id", "=", company.id), ("type", "=", "bank")],
    )
    if bank_accounts:
        missing = bank_accounts.filtered(lambda account: account not in bank_journals.default_account_id)
        if missing:
            add(
                "journal_bank",
                WARN,
                _("%s imported QuickBooks bank account(s) need a bank journal.") % len(missing),
                "auto",
            )
        return

    snapshot = mapping.configuration_snapshot if "configuration_snapshot" in mapping._fields else {}
    if (snapshot or {}).get("bank_accounts") and not bank_journals:
        add(
            "bank_configuration",
            REVIEW,
            _("QuickBooks reported bank accounts that still need configuration review."),
            "configuration",
        )


def _check_engine_support(add, mapping):
    from ..models.qbo_company_mapping import ENTITY_TYPES  # noqa: PLC0415

    labels = dict(ENTITY_TYPES)
    for entity_type, enabled in mapping._enabled_pull_entities():
        if not enabled:
            continue
        support = ENGINE_SUPPORT.get(entity_type, "none")
        if support == "full":
            continue
        add(
            f"engine_{entity_type}",
            WARN,
            _(ENGINE_GAP_MESSAGE[support]) % {"label": labels.get(entity_type, entity_type)},
        )


def _check_open_decisions(env, add, mapping, company):
    pending = env["qbo.conflict"].sudo().search_count(
        [("mapping_id", "=", mapping.id), ("status", "=", "pending")],
    )
    if pending:
        add(
            "conflicts_pending",
            REVIEW,
            _("%s sync conflicts need a human decision.") % pending,
            "manual",
        )

    if "poseidon.mapping.decision" not in env:
        return
    Account = env["account.account"].sudo()
    pulled_ids = set(Account.search(
        [("company_ids", "=", company.id), ("qbo_id", "!=", False)],
    ).mapped("qbo_id"))
    if not pulled_ids:
        return
    confirmed = env["poseidon.mapping.decision"].sudo().search_count(
        [
            ("company_id", "=", company.id),
            ("qbo_id", "in", list(pulled_ids)),
            ("state", "=", "confirmed"),
        ],
    )
    pulled = len(pulled_ids)
    if confirmed < pulled:
        remaining = pulled - confirmed
        add(
            "mapping_decisions",
            REVIEW,
            _("%(remaining)s of %(pulled)s imported QuickBooks accounts still need a mapping decision.")
            % {"remaining": remaining, "pulled": pulled},
            "mapping",
        )
