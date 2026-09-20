from odoo import _, api, fields, models
from odoo.exceptions import UserError


class PoseidonPropagationWizard(models.TransientModel):
    _name = "poseidon.mapping.propagation.wizard"
    _description = "Poseidon Mapping Propagation Wizard"

    decision_id = fields.Many2one(
        "poseidon.mapping.decision",
        required=True,
        ondelete="cascade",
    )
    group_id = fields.Many2one(
        "poseidon.company.group",
        string="Target group",
        required=True,
    )
    mode = fields.Selection(
        [
            ("requires_review", "Requires review"),
            ("automatic", "Automatic"),
        ],
        required=True,
        default="requires_review",
    )
    propagation_id = fields.Many2one(
        "poseidon.mapping.propagation",
        readonly=True,
        ondelete="set null",
    )
    line_ids = fields.One2many(
        related="propagation_id.line_ids",
        readonly=True,
    )
    can_commit = fields.Boolean(compute="_compute_can_commit")

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        decision_id = values.get("decision_id") or self.env.context.get("default_decision_id")
        if decision_id and not values.get("group_id"):
            decision = self.env["poseidon.mapping.decision"].browse(decision_id).exists()
            group = self.env["poseidon.company.group"].search(
                [
                    ("active", "=", True),
                    ("member_ids.company_id", "=", decision.company_id.id),
                    ("member_ids.active", "=", True),
                ],
                limit=1,
            )
            if group:
                values["group_id"] = group.id
        return values

    @api.depends("propagation_id.state", "propagation_id.line_ids.status")
    def _compute_can_commit(self):
        for wizard in self:
            wizard.can_commit = bool(
                wizard.propagation_id
                and wizard.propagation_id.state == "previewed"
                and not any(line.status == "conflict" for line in wizard.propagation_id.line_ids),
            )

    def action_preview(self):
        self.ensure_one()
        if self.mode == "automatic":
            raise UserError(_("Automatic propagation is not available in V1. Run a reviewed preview first."))
        propagation = self.propagation_id or self.env["poseidon.mapping.propagation"].create(
            {
                "decision_id": self.decision_id.id,
                "group_id": self.group_id.id,
                "mode": self.mode,
            },
        )
        propagation.action_build_preview()
        self.propagation_id = propagation.id
        return self._reload_action()

    def action_commit(self):
        self.ensure_one()
        if not self.propagation_id:
            raise UserError(_("Preview the propagation before committing it."))
        self.propagation_id.action_commit()
        return self._reload_action()

    def _reload_action(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Propagate Mapping"),
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }
