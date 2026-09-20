import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

L10N_US_WARNING = "Chart of Accounts (US GAAP) is not active"
# Chart-template codes that indicate an active US GAAP localization. In Odoo 17+
# res.company.chart_template is a Selection holding the template CODE: the stock
# code is "us" (registered by l10n_us_account), while Poseidon-provisioned
# companies carry "poseidon_qbo_us". Deployments can extend this set via the
# ir.config_parameter "poseidon.us_chart_template_codes" (comma-separated)
# without a code change.
US_CHART_TEMPLATE_CODES = ("us", "poseidon_qbo_us")
US_LOCALIZATION_MODULE = "l10n_us_account"


class PoseidonKernelVersion(models.Model):
    _name = "poseidon.kernel.version"
    _description = "Poseidon Kernel Version"
    _order = "installed_at desc, id desc"

    name = fields.Char(required=True)
    kernel_version = fields.Char(required=True, index=True)
    gaap_basis = fields.Selection(
        [("US_GAAP", "US GAAP")],
        required=True,
        default="US_GAAP",
    )
    kernel_layer = fields.Selection(
        [("L0", "L0"), ("L1", "L1"), ("L2", "L2"), ("L3", "L3")],
        required=True,
        default="L0",
    )
    status = fields.Selection(
        # Mirrors the artifact's own declared status; the importer never invents
        # one (see _kernel_status_from_metadata). "draft" exists for artifacts
        # still under revision — they stay replaceable, because the immutability
        # rules below apply to frozen records only. `active` is deliberately not
        # protected, so a frozen record can still be superseded by its successor.
        [("draft", "Draft"), ("frozen", "Frozen"), ("deprecated", "Deprecated")],
        required=True,
        default="frozen",
    )
    effective_from = fields.Date(required=True)
    installed_at = fields.Datetime(required=True, default=fields.Datetime.now, copy=False)
    checksum = fields.Char(required=True, index=True)
    active = fields.Boolean(default=True)
    notes = fields.Text()

    _kernel_version_layer_unique = models.Constraint(
        "UNIQUE(kernel_version, kernel_layer)",
        "A Poseidon kernel version/layer pair must be unique.",
    )

    def write(self, vals):
        protected = {
            "kernel_version",
            "gaap_basis",
            "kernel_layer",
            "status",
            "effective_from",
            "checksum",
        }
        if protected & vals.keys() and self.filtered(lambda record: record.status == "frozen"):
            raise UserError(
                _(
                    "Frozen kernel version records are immutable. "
                    "Create a new kernel version instead of changing this one.",
                ),
            )
        return super().write(vals)

    def unlink(self):
        if self.filtered(lambda record: record.status == "frozen"):
            raise UserError(_("Frozen kernel version records cannot be removed."))
        return super().unlink()

    @api.model
    def get_installed_kernel_status(self):
        version = self._get_active_kernel_version()
        return {
            "installed": bool(version),
            "kernel_version": version.kernel_version if version else None,
            "kernel_layer": version.kernel_layer if version else None,
            "gaap_basis": version.gaap_basis if version else None,
            "status": version.status if version else None,
            "checksum": version.checksum if version else None,
            "warnings": self.log_localization_warnings(),
        }

    @api.model
    def _get_active_kernel_version(self):
        # Prefer the record for the configured operational layer so the reported
        # "active layer" is meaningful when several layers of the same version
        # are seeded; otherwise fall back to the newest active record.
        default_layer = self.env["ir.config_parameter"].sudo().get_param(
            "poseidon.kernel.default_layer", "L1",
        )
        # L3 is never the installed kernel, whatever its status. It is an
        # optional posting-level expansion activated per company; the operational
        # kernel is always L0/L1/L2. This resolver falls back to "newest active
        # record of any layer", so on a database whose operational-layer record
        # is absent the L3 record would be reported as the authoritative kernel
        # and every gate that asks "is the kernel installed?" would answer yes,
        # naming 2025.3-L3.1 — which no version gate accepts, so the dashboard
        # would withhold metrics. Excluding by LAYER (not by draft status) is
        # what keeps that true now that the L3 artifact is FROZEN.
        normative = [
            ("active", "=", True),
            ("status", "!=", "draft"),
            ("kernel_layer", "!=", "L3"),
        ]
        preferred = self.search(
            normative + [("kernel_layer", "=", default_layer)],
            limit=1,
        )
        return preferred or self.search(normative, limit=1)

    @api.model
    def log_localization_warnings(self, company=None):
        target_company = (company or self.env.company).sudo()
        warnings = self._get_localization_warnings(target_company)
        for warning in warnings:
            _logger.warning(
                "Poseidon accounting kernel warning for company %s: %s",
                target_company.display_name,
                warning,
            )
        return warnings

    @api.model
    def _us_chart_template_codes(self):
        raw = self.env["ir.config_parameter"].sudo().get_param(
            "poseidon.us_chart_template_codes",
        )
        if raw:
            codes = tuple(code.strip() for code in raw.split(",") if code.strip())
            if codes:
                return codes
        return US_CHART_TEMPLATE_CODES

    @api.model
    def _get_localization_warnings(self, company):
        # l10n_us_account provides the US chart template and depends on l10n_us,
        # so its installed state is the authoritative DB-level signal that the US
        # localization is available — checking both modules was redundant.
        module = self.env["ir.module.module"].sudo().search(
            [("name", "=", US_LOCALIZATION_MODULE)],
            limit=1,
        )
        localization_installed = module.state == "installed"
        # Per-company signal: a US-appropriate chart of accounts is loaded.
        # chart_template is a Selection holding the template code (e.g. "us" or
        # the Poseidon-specific "poseidon_qbo_us"), NOT a legacy text field.
        company_on_us_chart = company.chart_template in self._us_chart_template_codes()
        l10n_us_active = localization_installed and company_on_us_chart
        return [] if l10n_us_active else [L10N_US_WARNING]
