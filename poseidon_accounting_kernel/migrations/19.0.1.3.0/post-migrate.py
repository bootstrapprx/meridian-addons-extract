"""Re-seed the L3 analytic kernel now that 2025.3-L3.1 is FROZEN.

Databases seeded from the DRAFT artifact carry three defects that only a
re-import clears:

* odoo_account_type was inferred from the account name. Every PP&E account was
  typed as a current asset, the AR aging buckets were not receivable and the
  trade AP detail was not payable. The artifact now declares the type.
* 16000 / 68000 / 68500 / 70000 carry L3 children but were imported as postable
  detail rows; they are headers.
* 13000 (Allowance for Doubtful Accounts) imported as a debit, because the
  frozen kernel states its polarity only via subcategory=CONTRA_ASSET.

The poseidon.kernel.version record for the layer still says `draft` and carries
the draft's checksum. The re-import refreshes it in place (see
_ensure_poseidon_kernel_versions) rather than recreating it: company accounts
FK-reference that row through account_account.poseidon_kernel_version_id, so on
any database with published accounts it cannot be deleted.
"""

import logging

import odoo

_logger = logging.getLogger(__name__)

L3_KERNEL_VERSION = "2025.3-L3.1"


def migrate(cr, version):
    env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
    if "qbo.standard.account" not in env:
        return

    stats = env["qbo.standard.account"].import_poseidon_l3_kernel()
    if not stats.get("available"):
        _logger.warning(
            "L3 re-seed skipped: kernel_v2025_3_L3_derived.json not found. The "
            "master chart keeps the DRAFT-era account types until the artifact "
            "is reachable (set poseidon.kernel.l3_json_path).",
        )
        return
    _logger.info(
        "L3 kernel 2025.3-L3.1 (FROZEN) re-seeded: created=%s updated=%s preserved=%s",
        stats.get("created", 0),
        stats.get("updated", 0),
        stats.get("preserved", 0),
    )
    record = env["poseidon.kernel.version"].sudo().search(
        [("kernel_version", "=", L3_KERNEL_VERSION), ("kernel_layer", "=", "L3")],
        limit=1,
    )
    if record.status != "frozen":
        raise odoo.exceptions.UserError(
            "The L3 kernel version record is still %r after the re-import. The "
            "artifact must declare status FROZEN and the record must follow it, "
            "or every kernel gate keeps reading a draft." % (record.status or "missing"),
        )

    # Fixing the master chart is not enough: company accounts published under
    # the DRAFT-era name heuristic keep their wrong account_type.
    realign = env["qbo.standard.account"].realign_l3_company_account_types()
    _logger.info(
        "L3 company account types realigned: updated=%s unchanged=%s blocked=%s",
        realign["updated"],
        realign["unchanged"],
        len(realign["blocked"]),
    )
    for code, current, declared in realign["blocked"]:
        _logger.warning(
            "L3 account %s is locked: account_type left at %s (kernel declares "
            "%s). Retyping a posted account moves its entries between statement "
            "lines — decide it explicitly.",
            code,
            current,
            declared,
        )

    # 16000/68000/68500/70000 became headers. Any company account already
    # published for them stays live and postable: unpublishing is a visible
    # change to a tenant's chart of accounts, so it is reported, not assumed.
    exposed = env["account.account"].sudo().with_context(active_test=False).search(
        [
            ("qbo_standard_account_id.kernel_layer", "=", "L3"),
            ("qbo_standard_account_id.entry_type", "=", "header"),
        ],
    )
    if exposed:
        _logger.warning(
            "%s company account(s) remain published for L3 header codes %s. The "
            "kernel now declares them non-postable. Retire them per company with "
            "account.chart.template.poseidon_deactivate_l3_accounts(company_id, codes) "
            "— it refuses any account carrying journal items.",
            len(exposed),
            # Not exposed.mapped("code"): account.account.code is
            # company-dependent in Odoo 19 and reads False without a company in
            # context. The kernel code is the same value and always set.
            ", ".join(sorted(set(exposed.mapped("qbo_standard_account_id.code")))),
        )
