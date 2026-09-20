from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.addons.qbo_bridge.models.qbo_account_bridge_rule import (
    account_type_guide_labels,
    mapping_match_reason,
)


class PoseidonMappingDecision(models.Model):
    _name = "poseidon.mapping.decision"
    _description = "Poseidon Mapping Decision"
    _order = "decision_at desc, id desc"

    name = fields.Char(compute="_compute_name", store=True)
    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("confirmed", "Confirmed"),
            ("rejected", "Rejected"),
        ],
        required=True,
        default="pending",
        index=True,
    )
    company_id = fields.Many2one(
        "res.company",
        required=True,
        index=True,
        ondelete="cascade",
    )
    source_account_id = fields.Many2one(
        "account.account",
        string="QBO account",
        required=True,
        index=True,
        ondelete="cascade",
    )
    destination_account_id = fields.Many2one(
        "account.account",
        string="Canonical destination",
        index=True,
        ondelete="restrict",
    )
    bridge_rule_id = fields.Many2one(
        "qbo.account.bridge.rule",
        string="Bridge rule",
        index=True,
        ondelete="restrict",
    )
    qbo_id = fields.Char(required=True, index=True)
    qbo_account_name = fields.Char()
    qbo_account_number = fields.Char()
    qbo_account_type = fields.Char()
    qbo_account_subtype = fields.Char()
    reason = fields.Text()
    decision_at = fields.Datetime(default=fields.Datetime.now, required=True)
    decision_by_id = fields.Many2one(
        "res.users",
        default=lambda self: self.env.user,
        required=True,
        ondelete="restrict",
    )

    # Didactic coach (Fase 3): why this suggestion exists and what the
    # destination is, in plain language. Computed, never stored — decisions are
    # immutable snapshots and the labels follow the current catalog.
    mapping_reason = fields.Char(
        string="Match reason",
        compute="_compute_mapping_coach",
        help="Why the bridge rule suggested this destination, in plain language.",
    )
    destination_normal_balance = fields.Char(
        string="Destination normal balance",
        compute="_compute_mapping_coach",
    )
    destination_normal_balance_label = fields.Char(
        string="Destination normal balance label",
        compute="_compute_mapping_coach",
    )
    destination_category = fields.Char(
        string="Destination category",
        compute="_compute_mapping_coach",
    )
    destination_type_label = fields.Char(
        string="Destination type",
        compute="_compute_mapping_coach",
    )
    destination_statement = fields.Char(
        string="Destination statement",
        compute="_compute_mapping_coach",
    )
    destination_is_l3 = fields.Boolean(
        string="Destination is analytic L3",
        compute="_compute_mapping_coach",
    )

    _company_qbo_unique = models.Constraint(
        "UNIQUE(company_id, qbo_id)",
        "Only one Poseidon mapping decision is allowed per company and QBO account.",
    )

    @api.depends("state", "qbo_account_name", "qbo_account_number", "company_id")
    def _compute_name(self):
        for decision in self:
            source = decision.qbo_account_number or decision.qbo_account_name or decision.qbo_id or _("QBO account")
            company = decision.company_id.display_name or _("Company")
            decision.name = _("%(source)s -> %(company)s (%(state)s)") % {
                "source": source,
                "company": company,
                "state": decision.state,
            }

    @api.constrains("state", "destination_account_id", "bridge_rule_id")
    def _check_confirmed_decision(self):
        for decision in self:
            if decision.state == "confirmed" and (
                not decision.destination_account_id or not decision.bridge_rule_id
            ):
                raise ValidationError(_("Confirmed mapping decisions require a destination account and a bridge rule."))

    @api.depends(
        "state",
        "reason",
        "bridge_rule_id",
        "bridge_rule_id.match_acct_num",
        "bridge_rule_id.match_name",
        "bridge_rule_id.match_account_type",
        "bridge_rule_id.match_account_subtype",
        "qbo_account_number",
        "qbo_account_name",
        "qbo_account_type",
        "qbo_account_subtype",
        "destination_account_id.account_type",
        "destination_account_id.qbo_standard_account_id",
        "destination_account_id.qbo_standard_account_id.kernel_layer",
    )
    def _compute_mapping_coach(self):
        for decision in self:
            if decision.bridge_rule_id:
                decision.mapping_reason = mapping_match_reason(
                    decision.bridge_rule_id,
                    {
                        "AcctNum": decision.qbo_account_number,
                        "Name": decision.qbo_account_name,
                        "AccountType": decision.qbo_account_type,
                        "AccountSubType": decision.qbo_account_subtype,
                    },
                    lang="pt",
                )
            elif decision.state == "pending":
                decision.mapping_reason = _("Aguardando sugestão")
            else:
                decision.mapping_reason = decision.reason or _("Mapeamento definido manualmente")
            guide = account_type_guide_labels(
                decision.destination_account_id.account_type
                if decision.destination_account_id
                else False,
                lang="pt",
            )
            decision.destination_normal_balance = guide.get("normal_balance") or False
            decision.destination_normal_balance_label = guide.get("normal_balance_label") or False
            decision.destination_category = guide.get("category") or False
            decision.destination_type_label = guide.get("type_label") or False
            decision.destination_statement = guide.get("statement") or False
            standard = (
                decision.destination_account_id.qbo_standard_account_id
                if decision.destination_account_id
                else False
            )
            decision.destination_is_l3 = bool(
                standard and "kernel_layer" in standard._fields and standard.kernel_layer == "L3"
            )

    @api.constrains("company_id", "source_account_id", "destination_account_id")
    def _check_company_scope(self):
        for decision in self:
            if decision.source_account_id and decision.company_id not in decision.source_account_id.company_ids:
                raise ValidationError(_("The QBO source account must belong to the decision company."))
            if decision.destination_account_id and decision.company_id not in decision.destination_account_id.company_ids:
                raise ValidationError(_("The destination account must belong to the decision company."))

    def _assert_company_period_unlocked(self, company):
        if company and (company.fiscalyear_lock_date or company.tax_lock_date):
            raise ValidationError(
                _(
                    "Mapping decisions cannot be changed while %(company)s has a locked accounting period."
                )
                % {"company": company.display_name}
            )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            company = self.env["res.company"].browse(vals.get("company_id")).exists()
            if not company and vals.get("source_account_id"):
                source = self.env["account.account"].browse(vals.get("source_account_id")).exists()
                company = source.company_ids[:1]
            self._assert_company_period_unlocked(company)
        return super().create(vals_list)

    def write(self, vals):
        if vals.get("company_id"):
            company = self.env["res.company"].browse(vals.get("company_id")).exists()
            self._assert_company_period_unlocked(company)
        else:
            for decision in self:
                self._assert_company_period_unlocked(decision.company_id)
        return super().write(vals)

    @api.model
    def record_mapping_decision(self, vals):
        source = self.env["account.account"].browse(vals.get("source_account_id")).exists()
        if not source:
            raise ValidationError(_("QBO source account is required."))
        qbo_id = source.qbo_id
        if not qbo_id:
            raise ValidationError(_("QBO source account has no QBO id."))

        company = self.env["res.company"].browse(vals.get("company_id")).exists()
        if not company:
            company = source.company_ids[:1]
        if not company:
            raise ValidationError(_("Mapping decision company is required."))
        self._assert_company_period_unlocked(company)

        state = vals.get("state") or "pending"
        values = {
            "state": state,
            "company_id": company.id,
            "source_account_id": source.id,
            "destination_account_id": vals.get("destination_account_id") or False,
            "bridge_rule_id": vals.get("bridge_rule_id") or False,
            "qbo_id": qbo_id,
            "qbo_account_name": source.qbo_source_name or source.name,
            "qbo_account_number": source.qbo_source_account_number or source.code,
            "qbo_account_type": source.qbo_source_account_type or source.account_type,
            "qbo_account_subtype": source.qbo_source_account_subtype,
            "reason": vals.get("reason") or False,
            "decision_at": fields.Datetime.now(),
            "decision_by_id": self.env.user.id,
        }
        decision = self.search(
            [
                ("company_id", "=", company.id),
                ("qbo_id", "=", qbo_id),
            ],
            limit=1,
        )
        if decision:
            decision.write(values)
        else:
            decision = self.create(values)
        return decision.id

    def action_open_propagation_wizard(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Propagate Mapping"),
            "res_model": "poseidon.mapping.propagation.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_decision_id": self.id,
            },
        }
