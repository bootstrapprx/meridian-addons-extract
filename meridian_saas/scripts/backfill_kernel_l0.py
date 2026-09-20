"""Backfill kernel L0 accounts into companies that have none.

Run via odoo shell, e.g.:
    odoo shell -d md-portfolio --no-http < backfill_kernel_l0.py
or paste interactively. Set DRY_RUN=False (env or edit) to commit.

Company 1 ("Md Portfolio", generic_coa) is intentionally skipped — it has a
working, non-kernel chart. Only companies with 0 kernel-linked accounts are
touched.
"""
import os

DRY_RUN = os.environ.get("BACKFILL_COMMIT", "0") != "1"

Account = env["account.account"].with_context(active_test=False)
ChartTemplate = env["account.chart.template"]

companies = env["res.company"].search([], order="id")
print(f"{'DRY-RUN' if DRY_RUN else 'LIVE'} backfill over {len(companies)} companies")
print("-" * 72)

for company in companies:
    linked = Account.search_count(
        [("company_ids", "=", company.id), ("qbo_standard_account_id", "!=", False)]
    )
    total = Account.search_count([("company_ids", "=", company.id)])
    if linked:
        print(f"[skip] id={company.id:>3} {company.name!r}: {linked} kernel / {total} total")
        continue
    if total:
        print(f"[skip] id={company.id:>3} {company.name!r}: {total} non-kernel accounts, left as-is")
        continue
    print(f"[seed] id={company.id:>3} {company.name!r}: 0 accounts -> seeding L0")
    if not DRY_RUN:
        stats = ChartTemplate.poseidon_publish_missing_standard_accounts(
            company.id, required_only=True
        )
        after = Account.search_count(
            [("company_ids", "=", company.id), ("qbo_standard_account_id", "!=", False)]
        )
        print(f"        -> created={stats['stats'].get('created')} now={after} kernel accounts")

if DRY_RUN:
    print("\nDRY-RUN only — nothing written. Re-run with BACKFILL_COMMIT=1 to apply.")
    env.cr.rollback()
else:
    env.cr.commit()
    print("\nLIVE run committed.")
