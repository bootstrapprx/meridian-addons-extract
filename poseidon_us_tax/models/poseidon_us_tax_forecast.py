import hashlib
import json

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class PoseidonUsTaxForecast(models.Model):
    _name = "poseidon.us.tax.forecast"
    _description = "Poseidon US Tax Forecast"
    _order = "generated_at desc, id desc"

    name = fields.Char(compute="_compute_name", store=True)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        index=True,
        ondelete="cascade",
    )
    currency_id = fields.Many2one(
        "res.currency",
        related="company_id.currency_id",
        readonly=True,
        store=True,
    )
    profile_id = fields.Many2one(
        "poseidon.us.tax.profile",
        required=True,
        index=True,
        ondelete="restrict",
    )
    ruleset_id = fields.Many2one(
        "poseidon.us.tax.ruleset",
        required=True,
        index=True,
        ondelete="restrict",
    )
    period_id = fields.Many2one("poseidon.kernel.period", index=True, ondelete="restrict")
    date_from = fields.Date(required=True, index=True)
    date_to = fields.Date(required=True, index=True)
    tax_year = fields.Integer(required=True, index=True)
    status = fields.Selection(
        [
            ("draft", "Draft"),
            ("reviewed", "Reviewed"),
            ("filed", "Filed outside Poseidon"),
        ],
        required=True,
        default="draft",
        index=True,
    )
    income_total = fields.Monetary(required=True, currency_field="currency_id")
    deduction_total = fields.Monetary(required=True, currency_field="currency_id")
    credit_total = fields.Monetary(required=True, currency_field="currency_id")
    adjustment_total = fields.Monetary(required=True, currency_field="currency_id")
    taxable_base = fields.Monetary(required=True, currency_field="currency_id")
    estimated_liability = fields.Monetary(required=True, currency_field="currency_id")
    effective_rate = fields.Float(required=True)
    confidence_score = fields.Integer(required=True, default=0)
    missing_inputs = fields.Json(default=list)
    drivers_json = fields.Json(default=list)
    inputs_hash = fields.Char(required=True, index=True)
    disclaimer = fields.Text(
        required=True,
        default=lambda self: self.env["poseidon.us.tax.ruleset"].planning_disclaimer(),
    )
    generated_at = fields.Datetime(required=True, default=fields.Datetime.now, copy=False)
    generated_by_id = fields.Many2one(
        "res.users",
        default=lambda self: self.env.user,
        required=True,
        readonly=True,
        ondelete="restrict",
    )
    reviewed_at = fields.Datetime(readonly=True, copy=False)
    reviewed_by_id = fields.Many2one("res.users", readonly=True, copy=False, ondelete="set null")
    filed_at = fields.Datetime(readonly=True, copy=False)
    filed_by_id = fields.Many2one("res.users", readonly=True, copy=False, ondelete="set null")
    fact_ids = fields.One2many("poseidon.us.tax.fact", "forecast_id", string="Tax facts")

    _disclaimer_required = models.Constraint(
        "CHECK(disclaimer IS NOT NULL AND length(disclaimer) > 0)",
        "Poseidon tax forecasts require a planning-only disclaimer.",
    )

    @api.depends("company_id", "tax_year", "status")
    def _compute_name(self):
        for forecast in self:
            company = forecast.company_id.display_name or _("Company")
            forecast.name = _("Tax planning %(year)s - %(company)s (%(status)s)") % {
                "year": forecast.tax_year or "",
                "company": company,
                "status": forecast.status,
            }

    @api.constrains("date_from", "date_to")
    def _check_dates(self):
        for forecast in self:
            if forecast.date_from and forecast.date_to and forecast.date_from > forecast.date_to:
                raise ValidationError(_("Forecast start must be on or before end date."))

    def write(self, vals):
        protected = {
            "company_id",
            "profile_id",
            "ruleset_id",
            "period_id",
            "date_from",
            "date_to",
            "tax_year",
            "income_total",
            "deduction_total",
            "credit_total",
            "adjustment_total",
            "taxable_base",
            "estimated_liability",
            "effective_rate",
            "confidence_score",
            "missing_inputs",
            "drivers_json",
            "inputs_hash",
            "disclaimer",
            "fact_ids",
        }
        if not self.env.context.get("poseidon_tax_generation"):
            if protected & vals.keys():
                raise UserError(_("Poseidon tax forecasts are generated records. Generate a new forecast instead of editing this one."))
            if "status" in vals:
                raise UserError(_("Use forecast actions to change the forecast review status."))
        return super().write(vals)

    def unlink(self):
        if self.filtered(lambda forecast: forecast.status in {"reviewed", "filed"}):
            raise UserError(_("Reviewed or externally filed tax forecasts cannot be removed."))
        return super().unlink()

    def action_mark_reviewed(self):
        for forecast in self:
            if forecast.status != "draft":
                raise UserError(_("Only draft forecasts can be reviewed."))
            forecast.with_context(poseidon_tax_generation=True).write(
                {
                    "status": "reviewed",
                    "reviewed_at": fields.Datetime.now(),
                    "reviewed_by_id": self.env.user.id,
                },
            )
        return True

    def action_mark_filed(self):
        for forecast in self:
            if forecast.status != "reviewed":
                raise UserError(_("Only reviewed forecasts can be marked as externally filed."))
            forecast.with_context(poseidon_tax_generation=True).write(
                {
                    "status": "filed",
                    "filed_at": fields.Datetime.now(),
                    "filed_by_id": self.env.user.id,
                },
            )
        return True

    @api.model
    def generate_from_accounting(self, company_id, date_from, date_to, ruleset_id=False, period_id=False):
        company = self.env["res.company"].browse(company_id).exists()
        if not company:
            raise UserError(_("Company is required for tax forecast generation."))

        period = self.env["poseidon.kernel.period"].browse(period_id).exists() if period_id else self.env["poseidon.kernel.period"]
        if period and period.state != "open":
            raise UserError(_("Tax forecast generation requires an open Poseidon accounting period."))

        profile = self.env["poseidon.us.tax.profile"].search(
            [
                ("company_id", "=", company.id),
                ("active", "=", True),
            ],
            limit=1,
        )
        if not profile:
            raise UserError(_("A Poseidon US tax profile is required before generating a forecast."))

        tax_year = fields.Date.to_date(date_to).year
        ruleset = self._select_ruleset(profile, tax_year, ruleset_id)
        if not ruleset:
            raise UserError(_("An active Poseidon US tax ruleset is required before generating a forecast."))

        facts = self._build_accounting_facts(company, date_from, date_to, tax_year, period)
        totals = self._calculate_totals(facts, profile, ruleset)
        forecast_values = {
            "company_id": company.id,
            "profile_id": profile.id,
            "ruleset_id": ruleset.id,
            "period_id": period.id if period else False,
            "date_from": date_from,
            "date_to": date_to,
            "tax_year": tax_year,
            "income_total": totals["income_total"],
            "deduction_total": totals["deduction_total"],
            "credit_total": totals["credit_total"],
            "adjustment_total": totals["adjustment_total"],
            "taxable_base": totals["taxable_base"],
            "estimated_liability": totals["estimated_liability"],
            "effective_rate": totals["effective_rate"],
            "confidence_score": totals["confidence_score"],
            "missing_inputs": totals["missing_inputs"],
            "drivers_json": totals["drivers"],
            "inputs_hash": self._inputs_hash(profile, ruleset, facts, totals),
            "disclaimer": self.env["poseidon.us.tax.ruleset"].planning_disclaimer(),
            "generated_at": fields.Datetime.now(),
            "generated_by_id": self.env.user.id,
        }
        forecast = self.with_context(poseidon_tax_generation=True).create(forecast_values)
        fact_values = []
        for fact in facts:
            fact_values.append(
                {
                    **fact,
                    "forecast_id": forecast.id,
                },
            )
        if fact_values:
            self.env["poseidon.us.tax.fact"].create(fact_values)
        return forecast.id

    def _select_ruleset(self, profile, tax_year, ruleset_id=False):
        if ruleset_id:
            ruleset = self.env["poseidon.us.tax.ruleset"].browse(ruleset_id).exists()
            if not ruleset or ruleset.status != "active":
                raise UserError(_("Selected Poseidon US tax ruleset is not active."))
            return ruleset

        domain = [
            ("status", "=", "active"),
            ("tax_year", "=", tax_year),
            ("entity_type", "in", [profile.entity_type, "all"]),
        ]
        if profile.state_code:
            domain = ["|", ("jurisdiction", "=", "federal"), ("state_code", "=", profile.state_code)] + domain
        else:
            domain = [("jurisdiction", "=", "federal")] + domain
        return self.env["poseidon.us.tax.ruleset"].search(domain, limit=1)

    def _build_accounting_facts(self, company, date_from, date_to, tax_year, period):
        groups = self.env["account.move.line"].read_group(
            [
                ("company_id", "=", company.id),
                ("parent_state", "=", "posted"),
                ("date", ">=", date_from),
                ("date", "<=", date_to),
            ],
            ["account_id", "balance:sum", "debit:sum", "credit:sum"],
            ["account_id"],
            lazy=False,
        )
        account_ids = [group["account_id"][0] for group in groups if group.get("account_id")]
        accounts = {account.id: account for account in self.env["account.account"].browse(account_ids)}
        facts = []
        for group in groups:
            account_ref = group.get("account_id")
            if not account_ref:
                continue
            account = accounts.get(account_ref[0])
            if not account:
                continue
            category = self._tax_category(account.account_type)
            if not category:
                continue
            amount = self._tax_amount(category, group.get("balance") or 0.0)
            if not amount:
                continue
            facts.append(
                {
                    "company_id": company.id,
                    "period_id": period.id if period else False,
                    "date_from": date_from,
                    "date_to": date_to,
                    "tax_year": tax_year,
                    "account_id": account.id,
                    "account_code": account.code,
                    "account_name": account.name,
                    "account_type": account.account_type,
                    "category": category,
                    "amount": amount,
                    "source": "odoo_actual",
                    "source_trace": {
                        "balance": group.get("balance") or 0.0,
                        "debit": group.get("debit") or 0.0,
                        "credit": group.get("credit") or 0.0,
                    },
                },
            )
        return facts

    def _tax_category(self, account_type):
        if account_type and account_type.startswith("income"):
            return "income"
        if account_type and account_type.startswith("expense"):
            return "deduction"
        return False

    def _tax_amount(self, category, balance):
        if category == "income":
            return round(-balance, 2)
        if category == "deduction":
            return round(balance, 2)
        return round(balance, 2)

    def _calculate_totals(self, facts, profile, ruleset):
        income_total = round(sum(fact["amount"] for fact in facts if fact["category"] == "income"), 2)
        deduction_total = round(sum(fact["amount"] for fact in facts if fact["category"] == "deduction"), 2)
        credit_total = round(sum(fact["amount"] for fact in facts if fact["category"] == "credit"), 2)
        adjustment_total = round(sum(fact["amount"] for fact in facts if fact["category"] == "adjustment"), 2)
        taxable_base = round(income_total - deduction_total + adjustment_total, 2)
        estimated_liability = self._estimate_liability(ruleset, taxable_base, credit_total)
        effective_rate = estimated_liability / taxable_base if taxable_base > 0 else 0.0
        missing_inputs = self._missing_inputs(profile, ruleset, facts)
        confidence_score = max(0, 100 - len(missing_inputs) * 12)
        drivers = sorted(
            [
                {
                    "account_id": fact.get("account_id"),
                    "account_code": fact.get("account_code"),
                    "account_name": fact["account_name"],
                    "category": fact["category"],
                    "amount": fact["amount"],
                }
                for fact in facts
            ],
            key=lambda item: abs(item["amount"]),
            reverse=True,
        )[:10]
        return {
            "income_total": income_total,
            "deduction_total": deduction_total,
            "credit_total": credit_total,
            "adjustment_total": adjustment_total,
            "taxable_base": taxable_base,
            "estimated_liability": estimated_liability,
            "effective_rate": effective_rate,
            "confidence_score": confidence_score,
            "missing_inputs": missing_inputs,
            "drivers": drivers,
        }

    def _estimate_liability(self, ruleset, taxable_base, credit_total):
        if taxable_base <= 0:
            return 0.0
        if ruleset.rate_model == "flat":
            return round(max(0.0, taxable_base * ruleset.flat_rate - credit_total), 2)
        if ruleset.rate_model == "brackets":
            return round(max(0.0, self._estimate_bracket_liability(ruleset.brackets_json or [], taxable_base) - credit_total), 2)
        return 0.0

    def _estimate_bracket_liability(self, brackets, taxable_base):
        liability = 0.0
        lower = 0.0
        for bracket in sorted(brackets, key=lambda item: item.get("up_to") or 10**18):
            upper = bracket.get("up_to") or taxable_base
            rate = bracket.get("rate") or 0.0
            if taxable_base <= lower:
                break
            span = min(taxable_base, upper) - lower
            liability += max(0.0, span) * rate
            lower = upper
        return liability

    def _missing_inputs(self, profile, ruleset, facts):
        missing = []
        if profile.entity_type == "unknown":
            missing.append("entity_type")
        if profile.tax_regime == "unknown":
            missing.append("tax_regime")
        if profile.accounting_method == "unknown":
            missing.append("accounting_method")
        if not profile.state_code:
            missing.append("state")
        if ruleset.rate_model == "none":
            missing.append("ruleset_rate_or_brackets")
        if not facts:
            missing.append("posted_income_or_expense_lines")
        return missing

    def _inputs_hash(self, profile, ruleset, facts, totals):
        payload = {
            "profile": {
                "id": profile.id,
                "entity_type": profile.entity_type,
                "tax_regime": profile.tax_regime,
                "accounting_method": profile.accounting_method,
                "state_code": profile.state_code,
            },
            "ruleset": {
                "id": ruleset.id,
                "version": ruleset.version,
                "rate_model": ruleset.rate_model,
                "flat_rate": ruleset.flat_rate,
                "brackets_json": ruleset.brackets_json,
            },
            "facts": facts,
            "totals": totals,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
