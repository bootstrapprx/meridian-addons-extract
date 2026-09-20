from odoo import fields, models


class PoseidonUsTaxFact(models.Model):
    _name = "poseidon.us.tax.fact"
    _description = "Poseidon US Tax Fact"
    _order = "date_from desc, category, id"

    forecast_id = fields.Many2one(
        "poseidon.us.tax.forecast",
        required=True,
        index=True,
        ondelete="cascade",
    )
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
    period_id = fields.Many2one("poseidon.kernel.period", index=True, ondelete="restrict")
    date_from = fields.Date(required=True, index=True)
    date_to = fields.Date(required=True, index=True)
    tax_year = fields.Integer(required=True, index=True)
    account_id = fields.Many2one("account.account", index=True, ondelete="restrict")
    account_code = fields.Char()
    account_name = fields.Char(required=True)
    account_type = fields.Char()
    category = fields.Selection(
        [
            ("income", "Income"),
            ("deduction", "Deduction"),
            ("credit", "Credit"),
            ("adjustment", "Adjustment"),
        ],
        required=True,
        index=True,
    )
    amount = fields.Monetary(required=True, currency_field="currency_id")
    source = fields.Selection(
        [
            ("odoo_actual", "Odoo actual"),
            ("scenario_projection", "Scenario projection"),
            ("manual_adjustment", "Manual adjustment"),
        ],
        required=True,
        default="odoo_actual",
        index=True,
    )
    source_trace = fields.Json(default=dict)
