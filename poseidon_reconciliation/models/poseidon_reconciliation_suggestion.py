from odoo import api, fields, models


class PoseidonReconciliationSuggestion(models.Model):
    _name = "poseidon.reconciliation.suggestion"
    _description = "Poseidon Reconciliation Suggestion"
    _order = "confidence desc, id desc"

    statement_line_id = fields.Many2one(
        "poseidon.bank.statement.line",
        required=True,
        index=True,
        ondelete="cascade",
    )
    company_id = fields.Many2one(
        "res.company",
        related="statement_line_id.company_id",
        store=True,
        readonly=True,
    )
    account_id = fields.Many2one(
        "account.account",
        required=True,
        index=True,
        ondelete="restrict",
    )
    amount = fields.Monetary(currency_field="currency_id")
    currency_id = fields.Many2one(
        "res.currency",
        related="statement_line_id.currency_id",
        readonly=True,
        store=True,
    )
    confidence = fields.Float(default=0.0)
    source = fields.Selection(
        [
            ("rule", "Rule"),
            ("history", "History"),
            ("ai", "AI"),
        ],
        required=True,
        default="history",
        index=True,
    )
    rationale = fields.Char()
    approved = fields.Boolean(default=False)

    @api.model_create_multi
    def create(self, vals_list):
        suggestions = super().create(vals_list)
        suggested_lines = suggestions.mapped("statement_line_id").filtered(lambda line: line.status == "unmatched")
        if suggested_lines:
            suggested_lines.write({"status": "suggested"})
        return suggestions
