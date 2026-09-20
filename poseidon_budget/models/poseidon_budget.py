# -*- coding: utf-8 -*-
# Part of Kodoo. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


class CrossoveredBudget(models.Model):
    _inherit = "crossovered.budget"

    def _poseidon_allowed_company(self, company_id):
        company = self.env["res.company"].browse(int(company_id or 0)).exists()
        if not company or company not in self.env.user.company_ids:
            raise AccessError("You do not have access to the requested company.")
        return company

    @api.model
    def _poseidon_line_dict(self, line):
        project = (
            line.analytic_account_id.project_ids[:1]
            if line.analytic_account_id
            else False
        )
        return {
            "id": line.id,
            "analytic_account_id": line.analytic_account_id.id or None,
            "analytic_account": line.analytic_account_id.name or "",
            "plan": line.analytic_plan_id.complete_name or "",
            "project_id": project.id or None,
            "project": project.name or "",
            "general_budget_id": line.general_budget_id.id or None,
            "general_budget": line.general_budget_id.name or "",
            "date_from": line.date_from.isoformat() if line.date_from else None,
            "date_to": line.date_to.isoformat() if line.date_to else None,
            "planned_amount": line.planned_amount or 0.0,
            "practical_amount": line.practical_amount or 0.0,
            "theoritical_amount": line.theoritical_amount or 0.0,
            "percentage": line.percentage or 0.0,
        }

    @api.model
    def _poseidon_budget_dict(self, budget):
        lines = budget.crossovered_budget_line
        line_dicts = [self._poseidon_line_dict(line) for line in lines]
        projects = {}
        for item in line_dicts:
            key = item["project_id"] or "unassigned"
            row = projects.setdefault(key, {
                "project_id": item["project_id"],
                "project": item["project"] or "Unassigned",
                "planned_amount": 0.0,
                "practical_amount": 0.0,
                "theoritical_amount": 0.0,
                "line_count": 0,
            })
            row["planned_amount"] += item["planned_amount"]
            row["practical_amount"] += item["practical_amount"]
            row["theoritical_amount"] += item["theoritical_amount"]
            row["line_count"] += 1
        return {
            "id": budget.id,
            "name": budget.name,
            "responsible_id": budget.user_id.id or None,
            "responsible": budget.user_id.name or "",
            "date_from": budget.date_from.isoformat() if budget.date_from else None,
            "date_to": budget.date_to.isoformat() if budget.date_to else None,
            "state": budget.state,
            "company_id": budget.company_id.id or None,
            "company": budget.company_id.name or "Shared",
            "lines": line_dicts,
            "projects": sorted(
                projects.values(),
                key=lambda item: (item["project_id"] is None, item["project"].lower()),
            ),
            "total_planned": sum(item["planned_amount"] for item in line_dicts),
            "total_practical": sum(item["practical_amount"] for item in line_dicts),
            "total_theoritical": sum(item["theoritical_amount"] for item in line_dicts),
        }

    @api.model
    def poseidon_budget_snapshot(self, company_id=False):
        """Budgets plus the allocation options for the same company, so the
        frontend can render the lifecycle in one round-trip."""
        company = self._poseidon_allowed_company(company_id or self.env.company.id)
        budgets = self.with_context(
            active_test=False, allowed_company_ids=[company.id]
        ).search([("company_id", "=", company.id)], order="date_from desc, id desc")
        items = [self._poseidon_budget_dict(budget) for budget in budgets]
        states = {}
        for item in items:
            states[item["state"]] = states.get(item["state"], 0) + 1
        analytic_accounts = self.env["account.analytic.account"].with_context(
            active_test=False
        ).search(
            ["|", ("company_id", "=", company.id), ("company_id", "=", False)],
            order="name, id",
        )
        budget_posts = self.env["account.budget.post"].search(
            [("company_id", "=", company.id)], order="name, id"
        )
        return {
            "items": items,
            "summary": {
                "count": len(items),
                "states": states,
                "total_planned": sum(item["total_planned"] for item in items),
            },
            "options": {
                "analytic_accounts": [
                    {
                        "id": account.id,
                        "name": account.name,
                        "code": account.code or "",
                        "plan": account.plan_id.complete_name or "",
                    }
                    for account in analytic_accounts
                ],
                "budget_posts": [
                    {"id": post.id, "name": post.name} for post in budget_posts
                ],
            },
        }

    @api.model
    def poseidon_create_budget(self, vals):
        company = self._poseidon_allowed_company(vals.get("company_id"))
        name = str(vals.get("name") or "").strip()
        if not name:
            raise UserError("Budget name is required.")
        date_from = fields.Date.to_date(vals.get("date_from"))
        date_to = fields.Date.to_date(vals.get("date_to"))
        if not date_from or not date_to:
            raise UserError("Budget start and end dates are required.")
        if date_to < date_from:
            raise ValidationError("The budget end date cannot precede its start date.")
        budget = self.with_company(company).with_context(
            allowed_company_ids=[company.id]
        ).create(
            {
                "name": name,
                "user_id": int(vals.get("user_id") or self.env.user.id),
                "date_from": date_from,
                "date_to": date_to,
                "company_id": company.id,
            }
        )
        return self._poseidon_budget_dict(budget)

    @api.model
    def poseidon_allocate_budget(self, budget_id, vals):
        budget = self.browse(int(budget_id or 0)).exists()
        if not budget:
            raise UserError("Budget not found.")
        company = self._poseidon_allowed_company(budget.company_id.id)
        if budget.state == "done":
            raise ValidationError("A closed budget cannot receive new allocations.")
        analytic = self.env["account.analytic.account"].browse(
            int(vals.get("analytic_account_id") or 0)
        ).exists()
        if not analytic:
            raise ValidationError("Choose an analytic account to allocate against.")
        if analytic.company_id and analytic.company_id != company:
            raise ValidationError(
                "The analytic account belongs to a different company."
            )
        post = self.env["account.budget.post"].browse(
            int(vals.get("general_budget_id") or 0)
        ).exists()
        if not post:
            raise ValidationError("Choose a budgetary position.")
        if post.company_id != company:
            raise ValidationError("The budgetary position belongs to a different company.")
        date_from = fields.Date.to_date(vals.get("date_from") or budget.date_from)
        date_to = fields.Date.to_date(vals.get("date_to") or budget.date_to)
        if not date_from or not date_to or date_to < date_from:
            raise ValidationError("Allocation dates are invalid.")
        planned = float(vals.get("planned_amount") or 0.0)
        if not planned:
            raise ValidationError("A planned amount is required.")
        line = self.env["crossovered.budget.lines"].with_company(company).with_context(
            allowed_company_ids=[company.id]
        ).create(
            {
                "crossovered_budget_id": budget.id,
                "analytic_account_id": analytic.id,
                "general_budget_id": post.id,
                "date_from": date_from,
                "date_to": date_to,
                "planned_amount": planned,
            }
        )
        budget.invalidate_recordset(["crossovered_budget_line"])
        return self._poseidon_budget_dict(budget)

    @api.model
    def poseidon_budget_action(self, budget_id, action):
        budget = self.browse(int(budget_id or 0)).exists()
        if not budget:
            raise UserError("Budget not found.")
        self._poseidon_allowed_company(budget.company_id.id)
        methods = {
            "draft": "action_budget_draft",
            "confirm": "action_budget_confirm",
            "validate": "action_budget_validate",
            "cancel": "action_budget_cancel",
            "done": "action_budget_done",
        }
        method = methods.get(action or "")
        if not method:
            raise ValidationError("Unknown budget action: %s" % action)
        getattr(budget, method)()
        return self._poseidon_budget_dict(budget)
