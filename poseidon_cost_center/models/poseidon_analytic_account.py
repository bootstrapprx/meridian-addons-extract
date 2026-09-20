# -*- coding: utf-8 -*-
# Part of Kodoo. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import date_utils

COST_CENTER_TYPES = [
    ("department", "Department"),
    ("property", "Property"),
    ("project", "Project"),
    ("shared_service", "Shared Service"),
    ("administrative", "Administrative"),
]

_POSEIDON_PLAN_REF = {
    "cost_center": ("poseidon_cost_center", "analytic_plan_cost_center"),
    "operational_object": ("industry_real_estate", "analytic_plan_properties"),
    "properties": ("industry_real_estate", "analytic_plan_properties"),
    "project": ("analytic", "analytic_plan_projects"),
}


class AccountAnalyticAccount(models.Model):
    _inherit = "account.analytic.account"

    poseidon_cost_center_type = fields.Selection(
        COST_CENTER_TYPES,
        string="Cost Center Type",
        default="department",
        help="Nature of the cost center for Meridian reporting.",
    )
    poseidon_cost_center_budget = fields.Monetary(
        string="Budget",
        currency_field="currency_id",
        help="Annual budget allocated to this cost center.",
    )
    poseidon_cost_center_external_ref = fields.Char(
        string="External Reference",
        help="Reference to the external system the center was mirrored from (e.g. QBO).",
    )
    poseidon_cost_center_owner = fields.Char(string="Owner")
    poseidon_cost_center_locked = fields.Boolean(string="Locked")
    poseidon_cost_center_lock_reason = fields.Char(string="Lock Reason")
    poseidon_object_center_id = fields.Many2one(
        "account.analytic.account",
        string="Rolls Up To Cost Center",
        help="Cost center that owns this operational object.",
    )

    def _poseidon_cost_center_plan(self):
        return self.env.ref("poseidon_cost_center.analytic_plan_cost_center", raise_if_not_found=False)

    def _poseidon_object_plan(self):
        return self.env["account.analytic.plan"]._poseidon_object_plan()

    def _poseidon_allowed_company(self, company_id):
        company = self.env["res.company"].browse(int(company_id or 0)).exists()
        if not company or company not in self.env.user.company_ids:
            raise AccessError("You do not have access to the requested company.")
        return company

    @api.constrains("code", "company_id", "root_plan_id")
    def _check_poseidon_cost_center_code_unique(self):
        plan = self._poseidon_cost_center_plan()
        for account in self.filtered(lambda item: plan and item.root_plan_id == plan and item.code):
            if self.search_count([
                ("id", "!=", account.id),
                ("code", "=", account.code),
                ("company_id", "=", account.company_id.id),
                ("root_plan_id", "=", plan.id),
            ], limit=1):
                raise ValidationError(
                    "Cost center code %s already exists in this company." % account.code
                )

    def _poseidon_item(self, account, plan):
        currency = account.currency_id or (account.company_id.currency_id if account.company_id else False)
        owner = account.poseidon_cost_center_owner
        if not owner and account.partner_id:
            owner = account.partner_id.name
        return {
            "id": account.id,
            "code": account.code or "",
            "name": account.name,
            "type": account.poseidon_cost_center_type,
            "owner": owner,
            "company": account.company_id.name if account.company_id else "Shared",
            "company_id": account.company_id.id if account.company_id else None,
            "currency": currency.name if currency else "USD",
            "budget": account.poseidon_cost_center_budget or 0.0,
            "debit": account.debit,
            "credit": account.credit,
            "balance": account.balance,
            "active": account.active,
            "locked": account.poseidon_cost_center_locked,
            "lock_reason": account.poseidon_cost_center_lock_reason or None,
            "external_ref": account.poseidon_cost_center_external_ref or None,
            "plan": plan.complete_name,
        }

    def _poseidon_plan_snapshot(self, plan, company_id=False):
        empty = {
            "items": [],
            "summary": {
                "count": 0,
                "active": 0,
                "locked": 0,
                "budget": 0.0,
                "balance": 0.0,
                "variance": 0.0,
            },
        }
        if not plan:
            return empty
        allowed_companies = self.env.user.company_ids
        domain = [("plan_id", "child_of", plan.id), ("active", "in", [True, False])]
        if company_id:
            company = self._poseidon_allowed_company(company_id)
            domain += ["|", ("company_id", "=", company.id), ("company_id", "=", False)]
            allowed_company_ids = [company.id]
        else:
            domain += ["|", ("company_id", "in", allowed_companies.ids), ("company_id", "=", False)]
            allowed_company_ids = allowed_companies.ids
        today = fields.Date.context_today(self)
        accounts = self.env["account.analytic.account"].with_context(
            active_test=False,
            allowed_company_ids=allowed_company_ids,
            from_date=date_utils.start_of(today, "year"),
            to_date=today,
        ).search(domain, order="code, name, id")
        items = [self._poseidon_item(account, plan) for account in accounts]
        budget = sum(item["budget"] for item in items)
        balance = sum(item["balance"] for item in items)
        return {
            "items": items,
            "summary": {
                "count": len(items),
                "active": sum(1 for item in items if item["active"]),
                "locked": sum(1 for item in items if item["locked"]),
                "budget": budget,
                "balance": balance,
                "variance": budget - abs(balance),
            },
        }

    @api.model
    def poseidon_cost_center_snapshot(self, company_id=False):
        return self._poseidon_plan_snapshot(self._poseidon_cost_center_plan(), company_id)

    @api.model
    def poseidon_object_snapshot(self, company_id=False):
        return self._poseidon_plan_snapshot(self._poseidon_object_plan(), company_id)

    @api.model
    def poseidon_create_cost_center(self, vals):
        plan = self._poseidon_cost_center_plan()
        if not plan:
            raise UserError("The Meridian cost center plan is not installed.")
        name = str(vals.get("name") or "").strip()
        code = str(vals.get("code") or "").strip()
        if not name:
            raise UserError("Cost center name is required.")
        company = self._poseidon_allowed_company(vals.get("company_id") or self.env.company.id)
        account = self.env["account.analytic.account"].with_company(company).with_context(
            allowed_company_ids=[company.id],
        ).create(
            {
                "name": name,
                "code": code or False,
                "plan_id": plan.id,
                "company_id": company.id,
                "poseidon_cost_center_type": vals.get("poseidon_cost_center_type") or "department",
                "poseidon_cost_center_budget": vals.get("poseidon_cost_center_budget") or 0.0,
                "poseidon_cost_center_external_ref": vals.get("poseidon_cost_center_external_ref") or False,
                "poseidon_cost_center_owner": vals.get("poseidon_cost_center_owner") or False,
            }
        )
        return self._poseidon_item(account, plan)

    @api.model
    def poseidon_seed_cost_center_template(self, company_id, dry_run=False):
        """Create the cost centers this company's activity implies.

        Idempotent by CODE within the company's cost-center plan: an existing
        code is left exactly as it is, never renamed or retyped, because the
        operator may have deliberately repurposed it. So this is safe to run on
        an established company — it fills gaps, it does not reconcile.

        The template lives with the activity catalog in poseidon_us_tax, which
        this module does not depend on: the lookup is soft, and a deployment
        without the tax module simply gets no template rather than an import
        error. `dry_run` returns what WOULD be created, for a preview.
        """
        plan = self._poseidon_cost_center_plan()
        if not plan:
            raise UserError("The Meridian cost center plan is not installed.")
        company = self._poseidon_allowed_company(company_id or self.env.company.id)

        if "poseidon.us.tax.profile" not in self.env:
            return {"available": False, "activity_tag": None, "created": [], "existing": []}
        profile = self.env["poseidon.us.tax.profile"].sudo().search(
            [("company_id", "=", company.id)], limit=1,
        )
        if not profile:
            return {"available": False, "activity_tag": None, "created": [], "existing": []}
        template = profile.poseidon_cost_center_template()

        # One search for the whole template: the per-code constraint would catch
        # a duplicate anyway, but raising on the second run is not idempotent.
        codes = [row["code"] for row in template]
        present = self.with_context(active_test=False).search([
            ("code", "in", codes),
            ("company_id", "=", company.id),
            ("root_plan_id", "=", plan.id),
        ])
        by_code = {account.code: account for account in present}

        created, existing = [], []
        for row in template:
            account = by_code.get(row["code"])
            if account:
                existing.append(self._poseidon_item(account, plan))
                continue
            if dry_run:
                created.append({"code": row["code"], "name": row["name"], "type": row["type"]})
                continue
            created.append(
                self.poseidon_create_cost_center(
                    {
                        "code": row["code"],
                        "name": row["name"],
                        "company_id": company.id,
                        "poseidon_cost_center_type": row["type"],
                    }
                )
            )
        return {
            "available": True,
            "activity_tag": profile.activity_tag,
            "dry_run": bool(dry_run),
            "created": created,
            "existing": existing,
        }

    @api.model
    def poseidon_create_analytic_dimension(self, vals):
        company = self._poseidon_allowed_company(vals.get("company_id") or self.env.company.id)
        plan = self.env["account.analytic.plan"].browse(int(vals.get("plan_id") or 0)).exists()
        if not plan:
            raise ValidationError("Choose an analytic plan.")
        plan.check_access("read")

        name = str(vals.get("name") or "").strip()
        code = str(vals.get("code") or "").strip()
        if not name:
            raise ValidationError("Analytic dimension name is required.")
        if len(name) > 256 or len(code) > 64:
            raise ValidationError("Analytic dimension code or name is too long.")
        if code and self.with_context(active_test=False).search_count([
            ("code", "=", code),
            ("company_id", "=", company.id),
            ("root_plan_id", "=", plan.id),
        ], limit=1):
            raise ValidationError("This analytic code already exists in the selected plan.")

        project_plan = self.env.ref("analytic.analytic_plan_projects", raise_if_not_found=False)
        cost_center_plan = self._poseidon_cost_center_plan()
        if plan == cost_center_plan:
            item = self.poseidon_create_cost_center({
                "name": name,
                "code": code,
                "company_id": company.id,
            })
            account = self.browse(item["id"])
        elif plan == project_plan:
            project = self.env["project.project"].with_company(company).with_context(
                allowed_company_ids=[company.id]
            ).create({"name": name, "company_id": company.id})
            project._create_analytic_account()
            account = project.account_id
            if code:
                account.code = code
        else:
            account = self.with_company(company).with_context(
                allowed_company_ids=[company.id]
            ).create({
                "name": name,
                "code": code or False,
                "plan_id": plan.id,
                "company_id": company.id,
            })
        return {
            "id": account.id,
            "code": account.code or "",
            "name": account.name,
            "plan_id": plan.id,
            "plan": plan.complete_name,
        }

    def write(self, vals):
        structural = {
            "active",
            "code",
            "company_id",
            "name",
            "plan_id",
            "poseidon_cost_center_external_ref",
            "poseidon_cost_center_type",
        }
        locked = self.filtered(
            lambda account: account.poseidon_cost_center_locked
            and account.root_plan_id == account._poseidon_cost_center_plan()
        )
        if locked and structural.intersection(vals):
            raise UserError("Locked cost centers cannot be structurally changed.")
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_locked_cost_center(self):
        if any(
            account.poseidon_cost_center_locked
            and account.root_plan_id == account._poseidon_cost_center_plan()
            for account in self
        ):
            raise UserError("Locked cost centers cannot be deleted.")


class AccountAnalyticPlan(models.Model):
    _inherit = "account.analytic.plan"

    @api.model
    def _poseidon_object_plan(self):
        return self.env.ref(
            "industry_real_estate.analytic_plan_properties", raise_if_not_found=False
        )

    @api.model
    def _poseidon_adopt_object_plan(self):
        properties = self._poseidon_object_plan()
        if not properties:
            return properties
        for xmlid in (
            "poseidon_cost_center.appl_cost_center_general",
            "poseidon_cost_center.appl_object_general",
        ):
            rule = self.env.ref(xmlid, raise_if_not_found=False)
            if rule:
                rule.unlink()
        legacy = self.env.ref(
            "poseidon_cost_center.analytic_plan_operational_object",
            raise_if_not_found=False,
        )
        if legacy and legacy != properties:
            legacy.account_ids.write({"plan_id": properties.id})
            legacy.unlink()
        if properties.name != "Operational Object" or properties.sequence != 30:
            properties.write({"name": "Operational Object", "sequence": 30})
        return properties

    @api.model
    def poseidon_analytic_plan_columns(self):
        result = {}
        for key, (module, xmlid) in _POSEIDON_PLAN_REF.items():
            plan = self.env.ref("%s.%s" % (module, xmlid), raise_if_not_found=False)
            result[key] = plan._column_name() if plan else False
        return result


class FleetVehicle(models.Model):
    _inherit = "fleet.vehicle"

    poseidon_object_account_id = fields.Many2one(
        "account.analytic.account",
        string="Operational Object",
        copy=False,
        help="Operational object account that vehicle costs and revenue are factored to.",
    )
    cost_center_id = fields.Many2one(
        "account.analytic.account",
        string="Cost Center",
        help="Cost center that owns this vehicle.",
    )

    def _poseidon_object_account(self):
        self.ensure_one()
        if not self.poseidon_object_account_id:
            self.poseidon_object_account_id = self.env["account.analytic.account"].create({
                "name": self.display_name,
                "code": self.license_plate or False,
                "plan_id": self.env["account.analytic.plan"]._poseidon_object_plan().id,
                "company_id": self.company_id.id,
                "poseidon_object_center_id": self.cost_center_id.id,
            })
        return self.poseidon_object_account_id

    def write(self, vals):
        result = super().write(vals)
        if "cost_center_id" in vals:
            for vehicle in self.filtered("poseidon_object_account_id"):
                vehicle.poseidon_object_account_id.poseidon_object_center_id = vehicle.cost_center_id
        return result


class MaintenanceEquipment(models.Model):
    _inherit = "maintenance.equipment"

    poseidon_object_account_id = fields.Many2one(
        "account.analytic.account",
        string="Operational Object",
        copy=False,
        help="Operational object account that equipment costs are factored to.",
    )
    cost_center_id = fields.Many2one(
        "account.analytic.account",
        string="Cost Center",
        help="Cost center that owns this equipment.",
    )

    def _poseidon_object_account(self):
        self.ensure_one()
        if not self.poseidon_object_account_id:
            self.poseidon_object_account_id = self.env["account.analytic.account"].create({
                "name": self.display_name,
                "plan_id": self.env["account.analytic.plan"]._poseidon_object_plan().id,
                "company_id": self.company_id.id,
                "poseidon_object_center_id": self.cost_center_id.id,
            })
        return self.poseidon_object_account_id

    def write(self, vals):
        result = super().write(vals)
        if "cost_center_id" in vals:
            for equipment in self.filtered("poseidon_object_account_id"):
                equipment.poseidon_object_account_id.poseidon_object_center_id = equipment.cost_center_id
        return result


class HrDepartment(models.Model):
    _inherit = "hr.department"

    cost_center_id = fields.Many2one(
        "account.analytic.account",
        string="Cost Center",
        help="Cost center that mirrors this department's expenses.",
    )


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    def _validate_analytic_distribution(self):
        return super(
            AccountMoveLine,
            self.with_context(validate_analytic=True),
        )._validate_analytic_distribution()

    @api.depends("vehicle_id")
    def _compute_analytic_distribution(self):
        return super()._compute_analytic_distribution()

    @api.depends("account_id")
    def _compute_need_vehicle(self):
        super()._compute_need_vehicle()
        for line in self:
            if (line.account_id.account_type or "").startswith("expense"):
                line.need_vehicle = True

    def _related_analytic_distribution(self):
        distribution = super()._related_analytic_distribution()
        self.ensure_one()
        if not self.vehicle_id:
            return distribution
        accounts = self.vehicle_id._poseidon_object_account()
        if self.vehicle_id.cost_center_id:
            accounts |= self.vehicle_id.cost_center_id
        return self._merge_distribution(distribution, {
            ",".join(map(str, accounts.ids)): 100.0,
            "__update__": accounts.root_plan_id.mapped(lambda plan: plan._column_name()),
        })
