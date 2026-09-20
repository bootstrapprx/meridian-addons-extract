from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class PoseidonCompanyGroup(models.Model):
    _name = "poseidon.company.group"
    _description = "Poseidon Company Group"
    _order = "name, id"

    name = fields.Char(required=True, index=True)
    code = fields.Char(index=True)
    active = fields.Boolean(default=True)
    currency_id = fields.Many2one(
        "res.currency",
        required=True,
        default=lambda self: self.env.company.currency_id,
        help="Consolidation display currency. Phase 15 assumes USD and does not perform FX conversion.",
    )
    member_ids = fields.One2many("poseidon.group.member", "group_id", string="Members")
    member_count = fields.Integer(compute="_compute_member_count", string="Member count")
    holding_company_id = fields.Many2one(
        "res.company",
        compute="_compute_holding_company",
        string="Holding company",
        store=True,
        readonly=True,
    )
    note = fields.Text()

    _code_unique = models.Constraint(
        "UNIQUE(code)",
        "Poseidon company group codes must be unique.",
    )

    @api.depends("member_ids.active")
    def _compute_member_count(self):
        for group in self:
            group.member_count = len(group.member_ids.filtered("active"))

    @api.depends("member_ids.active", "member_ids.role", "member_ids.company_id")
    def _compute_holding_company(self):
        for group in self:
            holding = group.member_ids.filtered(lambda member: member.active and member.role == "holding")[:1]
            group.holding_company_id = holding.company_id if holding else False

    @api.constrains("active")
    def _check_active_member_groups(self):
        self.filtered("active").member_ids._check_single_active_group()


class PoseidonGroupMember(models.Model):
    _name = "poseidon.group.member"
    _description = "Poseidon Group Member"
    _order = "group_id, sequence, company_id"

    group_id = fields.Many2one(
        "poseidon.company.group",
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
    role = fields.Selection(
        [
            ("holding", "Holding"),
            ("subsidiary", "Subsidiary"),
            ("affiliate", "Affiliate"),
        ],
        required=True,
        default="subsidiary",
        index=True,
    )
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    note = fields.Text()

    _group_company_unique = models.Constraint(
        "UNIQUE(group_id, company_id)",
        "A company can appear only once in the same Poseidon company group.",
    )

    @api.constrains("group_id", "role", "active")
    def _check_single_holding(self):
        for member in self:
            if not member.active or member.role != "holding" or not member.group_id:
                continue
            existing = self.search_count(
                [
                    ("id", "!=", member.id),
                    ("group_id", "=", member.group_id.id),
                    ("role", "=", "holding"),
                    ("active", "=", True),
                ],
                limit=1,
            )
            if existing:
                raise ValidationError(_("Only one active holding company is allowed per Poseidon company group."))

    @api.constrains("company_id", "group_id", "active")
    def _check_single_active_group(self):
        for member in self.filtered("active"):
            if not member.group_id.active:
                continue
            if self.search_count(
                [
                    ("id", "!=", member.id),
                    ("company_id", "=", member.company_id.id),
                    ("active", "=", True),
                    ("group_id.active", "=", True),
                ],
                limit=1,
            ):
                raise ValidationError(_("A company can belong to only one active Poseidon company group."))
