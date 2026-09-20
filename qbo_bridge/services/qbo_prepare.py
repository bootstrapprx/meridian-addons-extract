"""Create the safe, missing prerequisites for a QuickBooks pull.

Scope is deliberately narrow: journals only, including bank journals linked to
bank accounts already imported from QuickBooks. Accounts remain owned by the
frozen Poseidon kernel, so prepare never creates or mutates one.
"""
from odoo import _

from .qbo_readiness import PREPARABLE_JOURNALS, qbo_bank_accounts


def plan_prerequisites(env, mapping):
    """Return the journals that prepare would create. Never writes."""
    company = mapping.company_id
    journals = env["account.journal"].sudo().search([("company_id", "=", company.id)])
    present = set(journals.mapped("type"))
    taken = set(journals.mapped("code"))

    planned = []
    for key, journal_type, name, prefix, _toggle in PREPARABLE_JOURNALS:
        if journal_type in present:
            continue
        code = _free_code(taken, prefix)
        taken.add(code)
        planned.append(
            {
                "key": key,
                "model": "account.journal",
                "type": journal_type,
                "name": name,
                "code": code,
            },
        )

    linked_accounts = set(journals.filtered(lambda journal: journal.type == "bank").default_account_id.ids)
    for account in qbo_bank_accounts(env, company).filtered(lambda row: row.id not in linked_accounts):
        code = _free_code(taken, "BNK")
        taken.add(code)
        planned.append(
            {
                "key": f"journal_bank_{account.id}",
                "model": "account.journal",
                "type": "bank",
                "name": account.name,
                "code": code,
                "default_account_id": account.id,
            },
        )
    return planned


def apply_prerequisites(env, mapping):
    """Create the planned journals and log every one. Returns created rows."""
    company = mapping.company_id
    Journal = env["account.journal"].sudo()
    created = []
    for row in plan_prerequisites(env, mapping):
        vals = {
            "name": row["name"],
            "code": row["code"],
            "type": row["type"],
            "company_id": company.id,
        }
        if row.get("default_account_id"):
            vals["default_account_id"] = row["default_account_id"]
        journal = Journal.create(vals)
        created.append(dict(row, id=journal.id))
        env["qbo.sync.log"].log(
            env,
            mapping,
            "mapping",
            "pull",
            "success",
            "create",
            odoo_model="account.journal",
            odoo_record_id=journal.id,
            message=_("Prepared environment: created the %s journal '%s'.")
            % (row["type"], row["name"]),
        )
    return created


def summarise(env, mapping, created):
    """Write the summary log row that becomes the commit's audit_ref."""
    if created:
        message = _("Prepared environment: created %s record(s): %s.") % (
            len(created),
            ", ".join(f"{row['type']} journal {row['code']}" for row in created),
        )
    else:
        message = _("Prepared environment: nothing was missing.")
    return env["qbo.sync.log"].log(
        env, mapping, "mapping", "pull", "success", "create", message=message,
    )


def _free_code(taken, prefix):
    if prefix not in taken:
        return prefix
    counter = 1
    while f"{prefix}{counter}" in taken:
        counter += 1
    return f"{prefix}{counter}"
