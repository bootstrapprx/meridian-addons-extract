
## Account context and FinRobot calculations

The agent can request `poseidon.account_context` for a dated, company-scoped
account snapshot. Operator-authored descriptions and illustrative examples are
kept distinct from cited posted examples. The snapshot samples 15 transactions,
three posted entries and at most twelve counterpart lines per entry; it does
not train a model or write journal entries.

`poseidon.financial_calculation` is an optional read-only adapter for the
reviewed FinRobot V2 CAGR, WACC and DCF operators. It reuses upstream functions
and typed inputs rather than copying financial algorithms. The runtime verifies
the installed distribution version and VCS revision against
`requirements-finrobot.txt`. Without that dependency, the tool is unavailable.
Installing the similarly named V0 PyPI package does not activate it.

Inputs are explicitly labelled assumptions. Rates use decimal fractions; DCF
requires explicit tax, depreciation and terminal working-capital assumptions,
with at most twenty projection years. Results carry an input hash, engine,
version and reviewed revision. No market-provider calls, desktop server,
autonomous FinRobot agents or ledger writes are started by the adapter.

The broader upstream analysis, valuation, scenario and narrative-audit workflows
remain candidates for incremental adoption through the existing MCP boundary.
This first integration activates only the reviewed operators above; it does not
replace the Meridian agent runtime. See the [canonical architecture](../../../../context/architecture.md).

For the USGAAP Docker runtime, build the optional dependency into the image:

```sh
KODVS_USGAAP_FINROBOT=1 infra/scripts/compose-profile.sh usgaap build usgaap-backend
```

Persist `KODVS_USGAAP_FINROBOT=1` in the runtime environment to retain this option
on subsequent builds. The build installs the pinned upstream distribution with
Odoo's requirements as dependency constraints; a dependency conflict fails the
build. Backend and cron share this image. Building does not activate containers
or upgrade tenant databases. Use the profile deployment workflow afterwards.
Do not install dependencies interactively into running containers.

Offline adapter checks (no Odoo required):

```sh
python3 backend/addons/poseidon/poseidon_mcp/tests/test_finrobot_adapter.py
```

Set `KODVS_FINROBOT_SOURCE` to a reviewed V2 checkout's `finrobot_desktop`
directory to include the real upstream CAGR/WACC smoke checks.
