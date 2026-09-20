from odoo import fields, models


class AccountAccount(models.Model):
    _inherit = "account.account"

    qbo_standard_account_id = fields.Many2one(
        "qbo.standard.account",
        string="Master chart account",
        copy=False,
        ondelete="set null",
        index=True,
    )
