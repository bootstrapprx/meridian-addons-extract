from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

from ..models.poseidon_kernel_version import L10N_US_WARNING


@tagged("post_install", "-at_install", "poseidon_kernel")
class TestKernelLocalizationWarnings(TransactionCase):
    """Regression for the false 'l10n_us not active' warning (Workstream 3.2).

    Poseidon companies carry the chart_template code 'poseidon_qbo_us', not the
    stock 'us'. The previous hardcoded equality always failed, permanently
    flagging correctly configured US GAAP companies as 'Review needed'.
    """

    def _kernel(self):
        return self.env["poseidon.kernel.version"]

    def _warnings_with(self, chart_template, module_state):
        company = MagicMock()
        company.chart_template = chart_template
        module = MagicMock()
        module.state = module_state
        # Force the DB-level localization lookup to a known state so the test is
        # deterministic regardless of which modules the test DB has installed.
        module_model = self.env["ir.module.module"]
        with patch.object(type(module_model), "search", return_value=module):
            return self._kernel()._get_localization_warnings(company)

    def test_recognized_us_codes_include_poseidon(self):
        codes = self._kernel()._us_chart_template_codes()
        self.assertIn("us", codes)
        self.assertIn("poseidon_qbo_us", codes)

    def test_config_param_overrides_codes(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "poseidon.us_chart_template_codes", "alpha, beta ,"
        )
        self.assertEqual(
            self._kernel()._us_chart_template_codes(), ("alpha", "beta")
        )

    def test_no_warning_for_poseidon_company(self):
        self.assertEqual(self._warnings_with("poseidon_qbo_us", "installed"), [])

    def test_no_warning_for_stock_us_company(self):
        self.assertEqual(self._warnings_with("us", "installed"), [])

    def test_warning_when_chart_not_us(self):
        self.assertEqual(
            self._warnings_with("br", "installed"), [L10N_US_WARNING]
        )

    def test_warning_when_chart_unset(self):
        self.assertEqual(
            self._warnings_with(False, "installed"), [L10N_US_WARNING]
        )

    def test_warning_when_localization_module_missing(self):
        self.assertEqual(
            self._warnings_with("poseidon_qbo_us", "uninstalled"),
            [L10N_US_WARNING],
        )


@tagged("post_install", "-at_install", "poseidon_kernel")
class TestKernelVersionSeed(TransactionCase):
    """The seeded/active kernel must be the current frozen 2025.3, not 2025.2."""

    def test_active_kernel_is_2025_3(self):
        status = self.env["poseidon.kernel.version"].get_installed_kernel_status()
        self.assertTrue(status["installed"])
        self.assertEqual(status["kernel_version"], "2025.3")
        self.assertEqual(status["status"], "frozen")

    def test_prior_2025_2_is_deprecated_and_inactive(self):
        prior = self.env["poseidon.kernel.version"].with_context(
            active_test=False
        ).search([("kernel_version", "=", "2025.2")])
        # If present, it must be retired (fresh install seeds it inactive; the
        # migration retires it on existing databases).
        for record in prior:
            self.assertFalse(record.active)
            self.assertEqual(record.status, "deprecated")

    def test_2025_3_layers_are_frozen(self):
        versions = self.env["poseidon.kernel.version"].search(
            [("kernel_version", "=", "2025.3")]
        )
        self.assertTrue(versions)
        layers = set(versions.mapped("kernel_layer"))
        self.assertEqual(layers, {"L0", "L1", "L2"})
        self.assertTrue(all(v.status == "frozen" for v in versions))
