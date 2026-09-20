from odoo import api, models


class ResUsers(models.Model):
    _inherit = "res.users"

    @api.model
    def poseidon_operational_capabilities(self, company_id=None):
        models_by_tool = {
            "aiCenter": ("kodoo.ai.assistant",),
            "biDashboards": ("account.move.line",),
            "expenses": ("hr.expense",),
            "fleet": ("fleet.vehicle",),
            "invoicing": ("account.move",),
            "lunch": ("lunch.product", "lunch.order"),
            "maintenance": ("maintenance.equipment", "maintenance.request"),
            "people": ("hr.employee", "hr.attendance"),
            "project": (
                "project.project",
                "project.task",
                "checklist.item",
                "account.analytic.line",
            ),
            "realEstate": ("account.analytic.account", "sale.order"),
            "recruitment": ("hr.applicant", "hr.recruitment.stage"),
            "survey": ("survey.survey", "survey.user_input"),
        }
        company = self.env.company
        if company_id:
            try:
                company = self.env["res.company"].browse(int(company_id)).exists()
            except (TypeError, ValueError):
                company = self.env.company
        activity = "unknown"
        if company:
            profile = (
                self.env["poseidon.us.tax.profile"]
                .sudo()
                .search([("company_id", "=", company.id)], limit=1)
            )
            if profile:
                activity = profile.activity_tag or "unknown"
        tools_by_activity = self.env["poseidon.us.tax.profile"].activity_tools()
        allowed_by_activity = set(
            tools_by_activity.get(activity)
            or tools_by_activity.get("unknown")
            or []
        )
        return {
            tool: tool in allowed_by_activity
            and all(self.env[model].has_access("read") for model in model_names)
            for tool, model_names in models_by_tool.items()
        }
