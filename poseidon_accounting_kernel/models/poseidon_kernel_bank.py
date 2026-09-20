from odoo import fields, models


class ResBank(models.Model):
    """Institution master data used by the Poseidon BFF."""

    _inherit = "res.bank"

    institution_category = fields.Selection(
        [
            ("bank", "Bank"),
            ("card_provider", "Corporate card provider"),
        ],
        string="Institution category",
        default="bank",
        help="Master-data category shown when picking a bank or card institution.",
    )
