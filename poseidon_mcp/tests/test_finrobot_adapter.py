"""Offline adapter tests, runnable without Odoo (unittest discovery)."""
import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

source = os.environ.get("KODVS_FINROBOT_SOURCE")
if source:
    sys.path.insert(0, source)
spec = importlib.util.spec_from_file_location("finrobot_adapter", Path(__file__).parents[1] / "services" / "finrobot_adapter.py")
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class TestFinrobotAdapter(unittest.TestCase):
    def test_unavailable_engine_is_not_reported_as_computed(self):
        with patch.object(adapter, "availability", return_value={"available": False}):
            with self.assertRaises(RuntimeError):
                adapter.calculate("wacc", {})

    def test_invalid_rates_and_missing_assumptions_are_rejected_before_compute(self):
        with patch.object(adapter, "availability", return_value={"available": True}):
            with self.assertRaises(ValueError):
                adapter.calculate("wacc", {"risk_free_rate": float("nan")})
            with self.assertRaises(ValueError):
                adapter.calculate("dcf", {"revenue_base": 1000})
            with self.assertRaises(ValueError):
                adapter.calculate("cagr", {"start": 100, "end": 120, "years": True})

    def test_unreviewed_installation_stays_unavailable(self):
        with patch.object(adapter, "version", return_value="2.0.0"), patch.object(adapter, "distribution") as installed:
            installed.return_value.read_text.return_value = '{"vcs_info":{"commit_id":"unreviewed"}}'
            self.assertFalse(adapter.availability()["available"])

    @unittest.skipUnless(source, "Set KODVS_FINROBOT_SOURCE to the reviewed V2 source for upstream smoke tests")
    def test_real_upstream_cagr_and_wacc_preserve_traceability(self):
        # Test checkout is the inspected pinned revision. Production validates
        # installation metadata instead of relying on a source-tree path.
        with patch.object(adapter, "availability", return_value={"available": True}):
            cagr = adapter.calculate("cagr", {"start": 100, "end": 121, "years": 2})
            self.assertAlmostEqual(cagr["output"]["cagr"], 0.1)
            self.assertEqual(adapter.calculate("cagr", {"start": -100, "end": 121, "years": 2})["output"]["cagr"], None)
            inputs = {"risk_free_rate": .04, "beta": 1, "equity_risk_premium": .05, "cost_of_debt": .06, "tax_rate": .25, "debt_ratio": .4}
            output = adapter.calculate("wacc", inputs)
            self.assertAlmostEqual(output["output"]["wacc"], .072)
            self.assertEqual(output["input_hash"], adapter.calculate("wacc", inputs)["input_hash"])
            self.assertEqual(output["reviewed_revision"], adapter.UPSTREAM_REVISION)

    @unittest.skipUnless(source and importlib.util.find_spec("pydantic"), "Reviewed source and Pydantic are required for DCF smoke test")
    def test_real_upstream_dcf_uses_explicit_inputs_without_market_io(self):
        inputs = {"revenue_base": 1000, "revenue_growth_rates": [0., 0., 0.], "ebitda_margin": .2,
                  "capex_pct_revenue": .05, "nwc_pct_revenue": 0., "terminal_nwc_pct_revenue": 0.,
                  "da_pct_revenue": .02, "tax_rate": .25, "risk_free_rate": .04, "beta": 1.,
                  "equity_risk_premium": .05, "cost_of_debt": .06, "debt_ratio": .4,
                  "terminal_growth_rate": .02, "shares_outstanding": 100., "net_debt": 100.}
        with patch.object(adapter, "availability", return_value={"available": True}):
            result = adapter.calculate("dcf", inputs)
            self.assertGreater(result["output"]["enterprise_value"], 0)
            self.assertEqual(result["output"], adapter.calculate("dcf", inputs)["output"])
            self.assertEqual(result["inputs"]["tax_rate"], .25)


if __name__ == "__main__":
    unittest.main()
