"""Read-only FinRobot V2 operator adapter. No upstream code is copied."""
import hashlib
import importlib
import json
import math
from importlib.metadata import PackageNotFoundError, distribution, version

UPSTREAM_REVISION = "2717499b8e30f242640af08c4ad9afd1113c2d45"
OPERATORS = ("cagr", "wacc", "dcf")


def availability():
    try:
        installed = version("finrobot")
    except PackageNotFoundError:
        return {"available": False, "reason": "FinRobot V2 is not installed", "operators": list(OPERATORS)}
    if installed != "2.0.0":
        return {"available": False, "reason": "The reviewed FinRobot V2 package is required", "operators": list(OPERATORS)}
    try:
        origin = json.loads(distribution("finrobot").read_text("direct_url.json") or "{}")
    except (PackageNotFoundError, ValueError):
        return {"available": False, "reason": "FinRobot installation provenance is unavailable", "operators": list(OPERATORS)}
    if origin.get("vcs_info", {}).get("commit_id") != UPSTREAM_REVISION:
        return {"available": False, "reason": "Install the reviewed pinned FinRobot revision", "operators": list(OPERATORS)}
    return {"available": True, "version": installed, "operators": list(OPERATORS)}


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError("%s must be a finite number" % name)
    return value


def calculate(operator, inputs):
    if not availability()["available"]:
        raise RuntimeError("FinRobot V2 is unavailable. Install the reviewed optional requirements first.")
    if operator not in OPERATORS or not isinstance(inputs, dict):
        raise ValueError("Choose a supported FinRobot operator and structured inputs")
    encoded = json.dumps(inputs, sort_keys=True, allow_nan=False)
    if len(encoded) > 16000:
        raise ValueError("Financial inputs are too large")
    if operator == "cagr":
        if set(inputs) != {"start", "end", "years"}:
            raise ValueError("CAGR requires start, end and years")
        for key in ("start", "end", "years"):
            _number(inputs[key], key)
        if not isinstance(inputs["years"], int) or not 1 <= inputs["years"] <= 100:
            raise ValueError("Years must be an integer from 1 to 100")
        upstream = importlib.import_module("finrobot.engine.compute.operators.data_processor")
        output = {"cagr": upstream.calculate_cagr(**inputs)}
    elif operator == "wacc":
        keys = {"risk_free_rate", "beta", "equity_risk_premium", "cost_of_debt", "tax_rate", "debt_ratio"}
        if set(inputs) != keys:
            raise ValueError("WACC requires explicit rate, beta, tax and debt assumptions")
        for key in keys:
            value = _number(inputs[key], key)
            if not 0 <= value <= (5 if key == "beta" else 1):
                raise ValueError("%s is outside its permitted range" % key)
        upstream = importlib.import_module("finrobot.engine.compute.operators.wacc")
        equity, wacc = upstream.calculate_wacc(**inputs)
        output = {"cost_of_equity": equity, "wacc": wacc}
    else:
        # No fabricated US tax rate or D&A defaults: the operator must receive
        # explicitly reviewed inputs even where the upstream model has defaults.
        required = {"tax_rate", "da_pct_revenue", "terminal_nwc_pct_revenue"}
        if not required.issubset(inputs):
            raise ValueError("DCF requires explicit tax, depreciation and terminal working-capital assumptions")
        growth = inputs.get("revenue_growth_rates")
        if not isinstance(growth, list) or not 1 <= len(growth) <= 20:
            raise ValueError("DCF requires one to twenty projection years")
        models = importlib.import_module("finrobot.engine.models.financial")
        if set(inputs) - set(models.DCFInputs.model_fields):
            raise ValueError("Unknown DCF inputs")
        validated = models.DCFInputs.model_validate(inputs)
        upstream = importlib.import_module("finrobot.engine.compute.operators.dcf")
        output = upstream.calculate_dcf(validated).model_dump(mode="json")
    json.dumps(output, allow_nan=False)
    return {"engine": "FinRobot", "version": "2.0.0", "reviewed_revision": UPSTREAM_REVISION,
            "operator": operator, "input_hash": hashlib.sha256(encoded.encode()).hexdigest(),
            "inputs": inputs, "output": output, "policy": "Code-computed scenario from explicit inputs, not ledger truth or a posting instruction. Rates are decimal fractions. No market fetching, autonomous agents or writes were executed."}
