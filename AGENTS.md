# poseidon/ — Agent Guide

> US-GAAP accounting, QuickBooks Online bridge, and reconciliation add-ons for Odoo/Kodoo 19.

## Modules

| Module | Purpose |
| --- | --- |
| `qbo_bridge` | QuickBooks Online bridge; includes an isolated file-parser test suite (`test_qbo_file_parser.py`) that runs without an Odoo database. |
| `qbo_bridge_standard_chart` | L3 standard-chart master data sync; tested for import idempotence, layer preservation, individual/batch activation, reversible deactivation, and bulk-sync guarantees (see Gotchas). |
| `poseidon_accounting_kernel` | US accounting kernel — chart, periods, journal rules. `auto_install: True` on `l10n_us` + `l10n_us_account` + `qbo_bridge_standard_chart`. |
| `poseidon_reconciliation` | Bank reconciliation — sessions and matches. |
| `poseidon_mapping_governance` | Governance over account mapping decisions. |
| `poseidon_us_tax` | US tax handling. |
| `poseidon_company_group` | Multi-entity company grouping. |
| `poseidon_mcp` | Exposes Poseidon models over MCP. Extends `kodoo_mcp`. |
| `usgaap` | US GAAP umbrella app. Change entrypoints in `backend/addons/family-contracts.json`, regenerate profiles, then run `infra/scripts/sync-usgaap-app.py --write`; never hand-edit `profile.json` or manifest `depends`. |
| `poseidon_cost_center` | Cost-center and operational-object analytic plans, plus the Meridian RPCs over them. Ships **mandatory** analytic applicability for bills (`5,6`), invoices (`4`) and timesheets, so posting requires a distribution once installed. Reads the activity template from `poseidon_us_tax` through a soft lookup — no dependency, and `available: False` when that module is absent. |
| `poseidon_budget` | Meridian budget lifecycle over `crossovered.budget` (create, allocate, close, snapshot RPCs). Declares no model of its own; grants the Meridian accountant group access to budgets and budget lines. |
| `meridian_saas` | Tenant provisioning and the `provision_usgaap_kit()` entry point. |
| `meridian_onboarding` | Onboarding workflow; tested for the production guidance contract and mapping rationale (`test_onboarding.py`). |

## Domain rules

- Install Python dependencies declared by manifests (`requests`, `openpyxl`) into the active Odoo virtualenv before running these modules.
- Mock external services such as QuickBooks Online in tests; tests must not require live credentials or network access.
- Keep business logic in `models/` or `services/`; keep controllers thin. Prefer explicit record rules and access entries over implicit permissions.

## Build, test, run

```bash
./odoo-bin -c <config> -d <db> -u qbo_bridge --stop-after-init
./odoo-bin -c <config> -d <db> --test-enable -i qbo_bridge --stop-after-init
python -m pytest custom_addons/qbo_bridge/tests/test_qbo_file_parser.py -v
```

The Meridian + Kernel L3 suite (Fase 5) runs in the live `usgaap-backend` container. Because the running server owns port 8069, always pass `--http-port=0` (a plain `odoo-bin` run fails with "Address already in use"):

```sh
docker exec -i usgaap-backend sh -c 'cd /opt/odoo && timeout 800 ./odoo-bin \
  -c "$KODVS_RUNTIME/config/usgaap-backend/odoo.conf" \
  -d kodvs_usgaap --test-enable \
  -u meridian_onboarding,qbo_bridge_standard_chart \
  --stop-after-init --http-port=0 --log-level=info --test-tags=post_install'
```

68 `post_install` tests pass; exit code 0 means no failures (failures abort with exit 1).

## Gotchas

- The running server already owns port 8069 in the `usgaap-backend` container — always pass `--http-port=0`, or the run fails with "Address already in use".
- `qbo.standard.account.search()` does not accept a `count=` keyword in this codebase — use `search_count()` instead.
- Relevant suites: `qbo_bridge_standard_chart/tests/test_qbo_l3.py` (L3 master import idempotence/layer preservation, individual/batch activation, reversible deactivation, bulk sync never publishes L3) and `meridian_onboarding/tests/test_onboarding.py` (production guidance contract + mapping rationale).
- Do not hard-code credentials, realm secrets, database names, or local config files; keep OAuth/QBO credentials in Odoo records or environment-specific configuration.

## Related

- `context/architecture.md`
- `context/invariants.md`
- repo-root `AGENTS.md`
