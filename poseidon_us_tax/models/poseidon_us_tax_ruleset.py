from odoo import _, fields, models
from odoo.exceptions import UserError, ValidationError


class PoseidonUsTaxRuleset(models.Model):
    _name = "poseidon.us.tax.ruleset"
    _description = "Poseidon US Tax Ruleset"
    _order = "tax_year desc, version desc, id desc"

    name = fields.Char(required=True)
    version = fields.Char(required=True, index=True)
    tax_year = fields.Integer(required=True, index=True)
    jurisdiction = fields.Selection(
        [
            ("federal", "Federal"),
            ("state", "State"),
            ("local", "Local"),
        ],
        required=True,
        default="federal",
        index=True,
    )
    state_code = fields.Char(size=2)
    entity_type = fields.Selection(
        [
            ("all", "All"),
            ("llc", "LLC"),
            ("s_corp", "S-Corp"),
            ("c_corp", "C-Corp"),
            ("partnership", "Partnership"),
            ("sole_proprietor", "Sole proprietor"),
            ("unknown", "Unknown"),
        ],
        required=True,
        default="all",
        index=True,
    )
    scope = fields.Char(required=True, default="PASS_THROUGH_BASE", index=True)
    status = fields.Selection(
        [
            ("draft", "Draft"),
            ("active", "Active"),
            ("deprecated", "Deprecated"),
        ],
        required=True,
        default="draft",
        index=True,
    )
    rate_model = fields.Selection(
        [
            ("none", "No liability rate configured"),
            ("flat", "Flat planning rate"),
            ("brackets", "Bracket planning rate"),
        ],
        required=True,
        default="none",
    )
    flat_rate = fields.Float(default=0.0)
    brackets_json = fields.Json(default=list)
    rules_json = fields.Json(default=list)
    effective_from = fields.Date(required=True)
    effective_to = fields.Date()
    activated_at = fields.Datetime(readonly=True, copy=False)
    activated_by_id = fields.Many2one("res.users", readonly=True, copy=False, ondelete="set null")
    notes = fields.Text()

    _ruleset_unique = models.Constraint(
        "UNIQUE(version, scope, jurisdiction, state_code, entity_type)",
        "A Poseidon US tax ruleset version/scope/jurisdiction/entity combination must be unique.",
    )

    def write(self, vals):
        vals = dict(vals)
        if vals.get("state_code"):
            vals["state_code"] = vals["state_code"].strip().upper()
        protected = {
            "name",
            "version",
            "tax_year",
            "jurisdiction",
            "state_code",
            "entity_type",
            "scope",
            "rate_model",
            "flat_rate",
            "brackets_json",
            "rules_json",
            "effective_from",
            "effective_to",
        }
        if not self.env.context.get("poseidon_tax_ruleset_transition"):
            immutable = self.filtered(lambda ruleset: ruleset.status in {"active", "deprecated"})
            if immutable and protected & vals.keys():
                raise UserError(_("Active Poseidon tax rulesets are immutable. Create a new version instead."))
            if "status" in vals:
                raise UserError(_("Use the ruleset actions to activate or deprecate a Poseidon tax ruleset."))
        return super().write(vals)

    def unlink(self):
        if self.filtered(lambda ruleset: ruleset.status in {"active", "deprecated"}):
            raise UserError(_("Active or deprecated Poseidon tax rulesets cannot be removed."))
        return super().unlink()

    def action_activate(self):
        for ruleset in self:
            if ruleset.status != "draft":
                raise UserError(_("Only draft Poseidon tax rulesets can be activated."))
            ruleset._validate_rates()
            ruleset.with_context(poseidon_tax_ruleset_transition=True).write(
                {
                    "status": "active",
                    "activated_at": fields.Datetime.now(),
                    "activated_by_id": self.env.user.id,
                },
            )
        return True

    def action_deprecate(self):
        for ruleset in self:
            if ruleset.status != "active":
                raise UserError(_("Only active Poseidon tax rulesets can be deprecated."))
            ruleset.with_context(poseidon_tax_ruleset_transition=True).write({"status": "deprecated"})
        return True

    def _validate_rates(self):
        for ruleset in self:
            if ruleset.flat_rate < 0 or ruleset.flat_rate > 1:
                raise ValidationError(_("Flat planning rate must be between 0 and 1."))
            if ruleset.rate_model == "flat" and not ruleset.flat_rate:
                raise ValidationError(_("Flat planning rulesets require a flat rate."))
            if ruleset.rate_model == "brackets" and not ruleset.brackets_json:
                raise ValidationError(_("Bracket planning rulesets require bracket data."))

    @staticmethod
    def planning_disclaimer():
        return (
            "Estimated / projected tax exposure for planning only. "
            "This is not a tax filing, legal determination, or final tax obligation."
        )
