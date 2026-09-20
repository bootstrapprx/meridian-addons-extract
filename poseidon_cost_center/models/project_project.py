from odoo import api, models, _
from odoo.exceptions import UserError, ValidationError


class ProjectProject(models.Model):
    _inherit = "project.project"

    def _poseidon_cost_center_plan(self):
        return self.env.ref(
            "poseidon_cost_center.analytic_plan_cost_center",
            raise_if_not_found=False,
        )

    def _poseidon_cost_center(self):
        self.ensure_one()
        plan = self._poseidon_cost_center_plan()
        column = plan and plan._column_name()
        return self[column] if column and column in self._fields else False

    def _poseidon_cost_center_options(self):
        self.ensure_one()
        plan = self._poseidon_cost_center_plan()
        if not plan or not self.env["account.analytic.account"].has_access("read"):
            return []
        centers = self.env["account.analytic.account"].search([
            ("root_plan_id", "=", plan.id),
            ("company_id", "in", [False, self.company_id.id]),
            ("active", "=", True),
        ], order="code, name, id")
        return [{
            "id": center.id,
            "code": center.code or "",
            "name": center.name,
        } for center in centers]

    @api.model
    def poseidon_set_project_cost_center(self, project_id, cost_center_id=False):
        project = self.browse(int(project_id or 0)).exists()
        if not project:
            raise UserError(_("Project not found."))
        project.ensure_one()
        project.check_access("write")

        plan = project._poseidon_cost_center_plan()
        if not plan:
            raise UserError(_("The Meridian cost center plan is not installed."))
        column = plan._column_name()
        if column not in project._fields:
            raise UserError(_("The project cost center field is not available."))

        center = self.env["account.analytic.account"]
        if cost_center_id:
            center = center.browse(int(cost_center_id)).exists()
            if not center:
                raise UserError(_("Cost center not found."))
            center.ensure_one()
            center.check_access("read")
            if center.root_plan_id != plan:
                raise ValidationError(_("The selected analytic account is not a cost center."))
            if center.company_id and center.company_id != project.company_id:
                raise ValidationError(_("The project and cost center must belong to the same company."))

        previous = project[column]
        project.write({column: center.id or False})
        project.message_post(body=_(
            "Cost center changed from %(before)s to %(after)s.",
            before=previous.display_name if previous else _("Unassigned"),
            after=center.display_name if center else _("Unassigned"),
        ))
        return project._poseidon_project_economics_payload()

    def _poseidon_budget_payload(self):
        self.ensure_one()
        BudgetLine = self.env["crossovered.budget.lines"]
        if not self.account_id or not BudgetLine.has_access("read"):
            return {
                "can_view": False,
                "planned_costs": 0.0,
                "planned_revenues": 0.0,
                "actual_costs": 0.0,
                "actual_revenues": 0.0,
                "lines": [],
            }
        lines = BudgetLine.search([
            ("analytic_account_id", "=", self.account_id.id),
            ("crossovered_budget_state", "in", ["confirm", "validate", "done"]),
        ], order="date_from desc, date_to desc, id desc")
        rows = [{
            "id": line.id,
            "name": line.name,
            "date_from": str(line.date_from),
            "date_to": str(line.date_to),
            "planned": line.planned_amount,
            "actual": line.practical_amount,
            "theoretical": line.theoritical_amount,
            "achievement": line.percentage,
        } for line in lines]
        planned = [row["planned"] for row in rows]
        actual = [row["actual"] for row in rows]
        return {
            "can_view": True,
            "planned_costs": -sum(value for value in planned if value < 0),
            "planned_revenues": sum(value for value in planned if value > 0),
            "actual_costs": -sum(value for value in actual if value < 0),
            "actual_revenues": sum(value for value in actual if value > 0),
            "lines": rows,
        }

    def _poseidon_project_economics_payload(self):
        self.ensure_one()
        center = self._poseidon_cost_center()
        can_view_financials = self.env.user.has_group("project.group_project_manager")
        result = {
            "project_id": self.id,
            "name": self.name,
            "company_id": self.company_id.id,
            "company": self.company_id.name,
            "currency": self.currency_id.name,
            "cost_center": ({
                "id": center.id,
                "code": center.code or "",
                "name": center.name,
            } if center else None),
            "cost_center_options": self._poseidon_cost_center_options(),
            "can_edit": self.has_access("write"),
            "can_view_financials": can_view_financials,
            "costs": {"billed": 0.0, "committed": 0.0, "total": 0.0},
            "revenues": {"invoiced": 0.0, "expected": 0.0, "total": 0.0},
            "margin": 0.0,
            "margin_percentage": 0.0,
            "sections": [],
            "budget": self._poseidon_budget_payload() if can_view_financials else {
                "can_view": False,
                "planned_costs": 0.0,
                "planned_revenues": 0.0,
                "actual_costs": 0.0,
                "actual_revenues": 0.0,
                "lines": [],
            },
        }
        if not can_view_financials:
            return result

        items = self._get_profitability_items(with_action=False)
        labels = self._get_profitability_labels()
        cost_totals = items["costs"]["total"]
        revenue_totals = items["revenues"]["total"]
        billed_costs = abs(float(cost_totals.get("billed", 0.0)))
        committed_costs = abs(float(cost_totals.get("to_bill", 0.0)))
        invoiced_revenues = float(revenue_totals.get("invoiced", 0.0))
        expected_revenues = float(revenue_totals.get("to_invoice", 0.0))
        total_costs = billed_costs + committed_costs
        total_revenues = invoiced_revenues + expected_revenues
        margin = total_revenues - total_costs

        sections = []
        for kind, billed_key, pending_key in (
            ("cost", "billed", "to_bill"),
            ("revenue", "invoiced", "to_invoice"),
        ):
            bucket = items["costs" if kind == "cost" else "revenues"]
            for item in bucket["data"]:
                section_id = item.get("id", "other")
                sections.append({
                    "id": section_id,
                    "label": labels.get(section_id, str(section_id).replace("_", " ").title()),
                    "kind": kind,
                    "realized": abs(float(item.get(billed_key, 0.0))),
                    "pending": abs(float(item.get(pending_key, 0.0))),
                })

        result.update({
            "costs": {
                "billed": billed_costs,
                "committed": committed_costs,
                "total": total_costs,
            },
            "revenues": {
                "invoiced": invoiced_revenues,
                "expected": expected_revenues,
                "total": total_revenues,
            },
            "margin": margin,
            "margin_percentage": (margin / total_revenues * 100.0) if total_revenues else 0.0,
            "sections": sections,
        })
        return result

    @api.model
    def poseidon_project_economics(self, project_id):
        project = self.browse(int(project_id or 0)).exists()
        if not project:
            raise UserError(_("Project not found."))
        project.ensure_one()
        project.check_access("read")
        return project._poseidon_project_economics_payload()
