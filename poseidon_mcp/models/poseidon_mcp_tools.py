from odoo import models, api, _
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)

try:  # kodoo_ai_center is optional at runtime (other profiles may skip it)
    from kodoo_ai_center.models.company_scope import get_company_scope_ids
except ImportError:  # pragma: no cover - exercised only when the addon is absent
    get_company_scope_ids = None

_L3_BATCH_TYPES = ("activity_tag", "functional_group", "dashboard_account_set")


class PoseidonMCPTools(models.AbstractModel):
    _name = "poseidon.mcp.tools"
    _description = "Poseidon MCP Tools"

    @api.model
    def _execute_poseidon_health_check(self, job, payload):
        del job, payload
        installed = self.env["ir.module.module"].sudo().search(
            [
                ("name", "in", ["poseidon_accounting_kernel", "qbo_bridge_standard_chart"]),
                ("state", "=", "installed"),
            ]
        )
        installed_names = set(installed.mapped("name"))
        kernel_ready = {
            "poseidon_accounting_kernel",
            "qbo_bridge_standard_chart",
        }.issubset(installed_names)
        return {
            "status": "success" if kernel_ready else "error",
            "message": "Meridian kernel is ready." if kernel_ready else "Meridian kernel is not ready.",
            "details": {"kernel_ready": kernel_ready},
        }

    @api.model
    def _get_mcp_operation_catalog(self):
        return {
            "poseidon.health_check": {
                "key": "poseidon.health_check",
                "version": 1,
                "label": "Poseidon Health Check",
                "category": "poseidon",
                "bundle": "poseidon",
                "description": (
                    "Checks the health of the Meridian environment: whether the "
                    "Poseidon kernel and master chart modules are installed."
                ),
                "available": True,
                "heavy": False,
                "supports_process_now": True,
                "requires_modules": ["qbo_bridge_standard_chart"],
                "payload_outline": {
                    "required": [],
                    "optional": [],
                    "notes": ["No arguments are required."],
                },
                "result_outline": {
                    "status": "'success' or 'error'.",
                    "message": "Human-readable status.",
                    "details": {
                        "kernel_ready": "Whether the L3 kernel models are installed.",
                    },
                },
            },
            "poseidon.preview_l3_activation": {
                "key": "poseidon.preview_l3_activation",
                "version": 1,
                "label": "Preview L3 Activation",
                "category": "poseidon",
                "bundle": "poseidon",
                "description": (
                    "Read-only preview of activating L3 analytic accounts for a "
                    "company. Resolves the requested codes or batch selection, "
                    "validates the company under the caller, and reports count, "
                    "codes, already-present and blocked accounts. Never creates "
                    "or links accounts."
                ),
                "available": True,
                "heavy": False,
                "supports_process_now": True,
                "requires_modules": ["qbo_bridge_standard_chart"],
                "payload_outline": {
                    "required": ["company_id"],
                    "optional": ["codes", "batch_type", "batch_key"],
                    "notes": [
                        "Provide 'codes' (array of L3 master codes) OR "
                        "'batch_type' ('activity_tag' | 'functional_group' | "
                        "'dashboard_account_set') with optional 'batch_key'. "
                        "Preview never writes."
                    ],
                },
                "result_outline": {
                    "company_id": "Target company id.",
                    "company": "Target company name.",
                    "preview": "Always true for preview.",
                    "count": "Number of L3 codes in the selection.",
                    "codes": "Resolved L3 master codes.",
                    "already_present": "Codes already present in the company chart.",
                    "blocked": "Codes whose company account is kernel-locked.",
                    "source": "Selection origin (codes or batch).",
                },
            },
            "poseidon.activate_l3": {
                "key": "poseidon.activate_l3",
                "version": 1,
                "label": "Activate L3 Accounts",
                "category": "poseidon",
                "bundle": "poseidon",
                "description": (
                    "Activates approved L3 analytic accounts for a company: "
                    "creates or links the company account for each L3 master "
                    "code and wires the rollup parent. Requires the accounting "
                    "manager group. Idempotent via idempotency_key; retries "
                    "with the same key and payload never duplicate."
                ),
                "available": True,
                "heavy": False,
                "supports_process_now": True,
                "requires_modules": ["qbo_bridge_standard_chart"],
                "payload_outline": {
                    "required": ["company_id"],
                    "optional": ["codes", "batch_type", "batch_key"],
                    "notes": [
                        "Exactly the selection approved by the operator: "
                        "'codes' OR 'batch_type' (with optional 'batch_key'). "
                        "The backend revalidates the selection before writing."
                    ],
                },
                "result_outline": {
                    "company_id": "Target company id.",
                    "company": "Target company name.",
                    "activated": "Number of accounts processed.",
                    "accounts": "Per-account outcome (created/linked/already_present).",
                    "source": "Selection origin (codes or batch).",
                    "idempotency_key": "Job idempotency key for safe retries.",
                },
            },
        }

    # ------------------------------------------------------------------
    # L3 activation tools (Fase 8): preview + approved apply
    # ------------------------------------------------------------------

    @api.model
    def _poseidon_l3_company_in_scope(self, user, company):
        """Company scope: own company, allowed companies, or poseidon group peers."""
        if get_company_scope_ids is not None:
            return company.id in get_company_scope_ids(self.env, user.company_id.id)
        return company.id in {user.company_id.id, *user.company_ids.ids}

    @api.model
    def _poseidon_l3_company(self, payload):
        company = self.env["res.company"].browse(payload.get("company_id") or 0)
        if not company:
            raise UserError(_("Company not found."))
        if not self._poseidon_l3_company_in_scope(self.env.user, company):
            raise UserError(
                _("Company '%s' is outside your allowed scope.") % company.name,
            )
        return company

    @api.model
    def _poseidon_l3_selection(self, payload, company):
        """Resolve the payload into validated L3 master codes.

        Accepts either explicit ``codes`` or a batch selection. Validates codes
        against the L3 master chart (missing codes fail loudly, never partially)
        and returns ``(codes, source)`` with a JSON-safe origin description.
        """
        codes = payload.get("codes")
        batch_type = (payload.get("batch_type") or "").strip().lower()
        batch_key = payload.get("batch_key")

        if codes and batch_type:
            raise UserError(
                _("Provide either 'codes' or 'batch_type', not both."),
            )

        Chart = self.env["account.chart.template"]
        if batch_type:
            if batch_type not in _L3_BATCH_TYPES:
                raise UserError(
                    _(
                        "Unknown L3 batch type '%(batch)s'. Use activity_tag, "
                        "functional_group or dashboard_account_set.",
                    )
                    % {"batch": batch_type},
                )
            codes = Chart._l3_batch_codes(company, batch_type, batch_key)
            source = {
                "kind": "batch",
                "batch_type": batch_type,
                "batch_key": batch_key or None,
            }
        else:
            if isinstance(codes, str):
                codes = [part.strip() for part in codes.split(",") if part.strip()]
            codes = [str(code).strip() for code in (codes or []) if str(code).strip()]
            if not codes:
                raise UserError(
                    _("L3 activation requires 'codes' or a 'batch_type' selection."),
                )
            standards = Chart._l3_standards_by_codes(codes)
            found = {standard.code for standard in standards}
            missing = sorted(set(codes) - found)
            if missing:
                raise UserError(
                    _(
                        "L3 account codes not found in the master chart: %(codes)s. "
                        "L3 accounts must exist in the kernel before activation.",
                    )
                    % {"codes": ", ".join(missing)},
                )
            source = {"kind": "codes"}
        return list(codes), source

    @api.model
    def _poseidon_l3_presence(self, company, codes):
        """Codes already present in the company chart and kernel-locked ones."""
        accounts = (
            self.env["account.account"]
            .with_company(company)
            .with_context(active_test=False)
            .search([("company_ids", "=", company.id), ("code", "in", codes)])
        )
        present = accounts.mapped("code")
        blocked = [
            account.code
            for account in accounts
            if "poseidon_kernel_locked" in account._fields
            and account.poseidon_kernel_locked
        ]
        return sorted(present), sorted(blocked)

    @api.model
    def _execute_poseidon_l3_preview(self, job, payload):
        del job
        company = self._poseidon_l3_company(payload)
        codes, source = self._poseidon_l3_selection(payload, company)
        already_present, blocked = (
            self._poseidon_l3_presence(company, codes) if codes else ([], [])
        )
        return {
            "company_id": company.id,
            "company": company.name,
            "preview": True,
            "count": len(codes),
            "codes": list(codes),
            "already_present": already_present,
            "blocked": blocked,
            "source": source,
        }

    @api.model
    def _execute_poseidon_l3_activate(self, job, payload):
        user = self.env.user
        if not user.has_group("account.group_account_manager"):
            raise UserError(
                _("L3 account activation requires the accounting manager group."),
            )
        company = self._poseidon_l3_company(payload)
        batch_type = (payload.get("batch_type") or "").strip().lower()
        Chart = self.env["account.chart.template"].with_user(user)
        job._set_progress(
            total=1,
            current=0,
            message=_("Activating L3 accounts for %s...") % company.name,
        )

        if batch_type:
            result = Chart.poseidon_activate_l3_batch(
                company.id,
                batch_type,
                payload.get("batch_key"),
                preview=False,
            )
            result["source"] = {
                "kind": "batch",
                "batch_type": batch_type,
                "batch_key": payload.get("batch_key") or None,
            }
        else:
            codes, source = self._poseidon_l3_selection(payload, company)
            result = Chart.poseidon_activate_l3_accounts(company.id, codes)
            result["source"] = source

        result["idempotency_key"] = job.idempotency_key
        if result.get("accounts"):
            job._set_target("account.account", result["accounts"][0]["account"]["id"])
        job._advance_progress(message=_("L3 activation completed for %s.") % company.name)
        return result


class KodooMCPJob(models.Model):
    _inherit = "kodoo.mcp.job"

    @api.model
    def _operation_catalog(self):
        catalog = super()._operation_catalog()
        if "poseidon.mcp.tools" in self.env:
            catalog.update(self.env["poseidon.mcp.tools"]._get_mcp_operation_catalog())
        return catalog

    def _execute_operation(self, user, payload):
        if self.operation_key and self.operation_key.startswith("poseidon."):
            if "poseidon.mcp.tools" in self.env:
                tools = self.env["poseidon.mcp.tools"].with_user(user)
                if self.operation_key == "poseidon.health_check":
                    return tools._execute_poseidon_health_check(self, payload)
                if self.operation_key == "poseidon.preview_l3_activation":
                    return tools._execute_poseidon_l3_preview(self, payload)
                if self.operation_key == "poseidon.activate_l3":
                    return tools._execute_poseidon_l3_activate(self, payload)
                raise UserError(
                    _("Unsupported poseidon MCP operation '%s'.") % self.operation_key,
                )
        return super()._execute_operation(user, payload)
