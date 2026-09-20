from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class PoseidonMappingPropagation(models.Model):
    _name = "poseidon.mapping.propagation"
    _description = "Poseidon Mapping Propagation"
    _order = "create_date desc, id desc"

    name = fields.Char(compute="_compute_name", store=True)
    decision_id = fields.Many2one(
        "poseidon.mapping.decision",
        required=True,
        index=True,
        ondelete="cascade",
    )
    source_company_id = fields.Many2one(
        "res.company",
        related="decision_id.company_id",
        store=True,
        readonly=True,
    )
    group_id = fields.Many2one(
        "poseidon.company.group",
        string="Target group",
        required=True,
        index=True,
        ondelete="restrict",
    )
    mode = fields.Selection(
        [
            ("requires_review", "Requires review"),
            ("automatic", "Automatic"),
        ],
        required=True,
        default="requires_review",
    )
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("previewed", "Previewed"),
            ("applied", "Applied"),
            ("blocked", "Blocked"),
            ("cancelled", "Cancelled"),
        ],
        required=True,
        default="draft",
        index=True,
    )
    line_ids = fields.One2many(
        "poseidon.mapping.propagation.line",
        "propagation_id",
        string="Preview lines",
    )
    previewed_at = fields.Datetime(readonly=True)
    committed_at = fields.Datetime(readonly=True)
    created_by_id = fields.Many2one(
        "res.users",
        default=lambda self: self.env.user,
        required=True,
        readonly=True,
        ondelete="restrict",
    )
    note = fields.Text()

    @api.depends("decision_id", "group_id", "state")
    def _compute_name(self):
        for propagation in self:
            decision_name = propagation.decision_id.display_name or _("Mapping decision")
            group_name = propagation.group_id.display_name or _("Group")
            propagation.name = _("%(decision)s -> %(group)s (%(state)s)") % {
                "decision": decision_name,
                "group": group_name,
                "state": propagation.state,
            }

    @api.constrains("decision_id")
    def _check_decision_confirmed(self):
        for propagation in self:
            if propagation.decision_id.state != "confirmed":
                raise ValidationError(_("Only confirmed mapping decisions can be propagated."))

    def action_build_preview(self):
        for propagation in self:
            propagation._build_preview()
        return True

    def action_commit(self):
        for propagation in self:
            propagation._commit_preview()
        return True

    def _build_preview(self):
        self.ensure_one()
        if self.state not in ("draft", "previewed", "blocked"):
            raise UserError(_("Only draft or previewed propagations can be previewed."))
        decision = self.decision_id
        if decision.state != "confirmed":
            raise UserError(_("Confirm the mapping before propagating it."))
        if not decision.bridge_rule_id or not decision.destination_account_id:
            raise UserError(_("The confirmed decision must include a bridge rule and destination account."))

        self.line_ids.unlink()
        members = self.group_id.member_ids.filtered("active").sorted(key=lambda member: (member.sequence, member.id))
        if not members:
            raise UserError(_("The target group has no active members."))

        lines = []
        for member in members:
            values = self._prepare_preview_line(member.company_id)
            lines.append((0, 0, values))
        self.write(
            {
                "line_ids": lines,
                "state": "previewed",
                "previewed_at": fields.Datetime.now(),
            },
        )

    def _prepare_preview_line(self, company):
        self.ensure_one()
        decision = self.decision_id
        values = {
            "company_id": company.id,
            "qbo_id": decision.qbo_id,
            "qbo_account_name": decision.qbo_account_name,
            "status": "pending",
            "message": _("Ready to apply after human approval."),
        }

        if company == decision.company_id:
            values.update(
                {
                    "status": "skipped",
                    "source_account_id": decision.source_account_id.id,
                    "target_account_id": decision.destination_account_id.id,
                    "existing_rule_id": decision.bridge_rule_id.id,
                    "message": _("Source company already owns this mapping decision."),
                },
            )
            return values

        if self._company_has_locked_period(company):
            values.update(
                {
                    "status": "conflict",
                    "message": self._company_lock_message(company),
                },
            )
            return values

        source_account = self._find_target_qbo_account(company)
        if not source_account:
            values.update(
                {
                    "status": "skipped",
                    "message": _("No imported QBO account with this QBO id exists for the target company."),
                },
            )
            return values
        values["source_account_id"] = source_account.id

        destination_account = self._find_target_destination_account(company)
        if not destination_account:
            values.update(
                {
                    "status": "skipped",
                    "message": _("No matching canonical destination account exists for the target company."),
                },
            )
            return values
        values["target_account_id"] = destination_account.id
        if self._destination_is_locked(destination_account):
            values.update(
                {
                    "status": "conflict",
                    "message": _("The target canonical destination account is locked or already has journal items."),
                },
            )
            return values

        existing_decision = self.env["poseidon.mapping.decision"].search(
            [
                ("company_id", "=", company.id),
                ("qbo_id", "=", decision.qbo_id),
            ],
            limit=1,
        )
        if existing_decision:
            values["existing_decision_id"] = existing_decision.id
            if (
                existing_decision.state == "confirmed"
                and existing_decision.bridge_rule_id == decision.bridge_rule_id
                and existing_decision.destination_account_id == destination_account
            ):
                values.update(
                    {
                        "status": "skipped",
                        "existing_rule_id": existing_decision.bridge_rule_id.id,
                        "message": _("Target company already has this mapping decision."),
                    },
                )
                return values
            values.update(
                {
                    "status": "conflict",
                    "existing_rule_id": existing_decision.bridge_rule_id.id,
                    "message": _("Target company already has a different mapping decision for this QBO account."),
                },
            )
            return values

        existing_rule = self._find_existing_rule_for_source(source_account)
        if existing_rule:
            values["existing_rule_id"] = existing_rule.id
            if not self._bridge_rule_has_same_canonical_target(existing_rule):
                values.update(
                    {
                        "status": "conflict",
                        "message": _("A bridge rule already maps this QBO account to a different canonical account."),
                    },
                )
                return values

        values["new_rule_id"] = decision.bridge_rule_id.id
        return values

    def _commit_preview(self):
        self.ensure_one()
        if self.state != "previewed":
            raise UserError(_("Build and review the preview before committing propagation."))
        if any(line.status == "conflict" for line in self.line_ids):
            self.state = "blocked"
            raise UserError(_("Propagation has conflicts. Resolve them before applying any mapping."))

        pending_lines = self.line_ids.filtered(lambda line: line.status == "pending")
        locked_lines = pending_lines.filtered(lambda line: self._company_has_locked_period(line.company_id))
        if locked_lines:
            self.state = "blocked"
            locked_company_names = ", ".join(locked_lines.mapped("company_id.display_name"))
            raise UserError(
                _(
                    "Propagation targets locked accounting period(s): %(companies)s. "
                    "Unlock the company period before committing."
                )
                % {"companies": locked_company_names}
            )
        for line in pending_lines:
            self.env["poseidon.mapping.decision"].record_mapping_decision(
                {
                    "company_id": line.company_id.id,
                    "source_account_id": line.source_account_id.id,
                    "destination_account_id": line.target_account_id.id,
                    "bridge_rule_id": self.decision_id.bridge_rule_id.id,
                    "state": "confirmed",
                    "reason": _("Propagated from decision %(decision)s by propagation %(propagation)s.")
                    % {
                        "decision": self.decision_id.display_name,
                        "propagation": self.display_name,
                    },
                },
            )
            line.write(
                {
                    "status": "applied",
                    "new_rule_id": self.decision_id.bridge_rule_id.id,
                    "message": _("Mapping applied after human approval."),
                },
            )
        self.write(
            {
                "state": "applied",
                "committed_at": fields.Datetime.now(),
            },
        )

    def _find_target_qbo_account(self, company):
        self.ensure_one()
        return self.env["account.account"].search(
            [
                ("company_ids", "in", [company.id]),
                ("qbo_id", "=", self.decision_id.qbo_id),
            ],
            limit=1,
        )

    def _find_target_destination_account(self, company):
        self.ensure_one()
        source_destination = self.decision_id.destination_account_id
        domain = [
            ("company_ids", "in", [company.id]),
            ("active", "=", True),
        ]
        if source_destination.code:
            match = self.env["account.account"].search(domain + [("code", "=", source_destination.code)], limit=1)
            if match:
                return match
        return self.env["account.account"].search(
            domain
            + [
                ("name", "=", source_destination.name),
                ("account_type", "=", source_destination.account_type),
            ],
            limit=1,
        )

    def _find_existing_rule_for_source(self, source_account):
        self.ensure_one()
        record = {
            "AcctNum": source_account.qbo_source_account_number or source_account.code,
            "Name": source_account.qbo_source_name or source_account.name,
            "AccountType": source_account.qbo_source_account_type or source_account.account_type,
            "AccountSubType": source_account.qbo_source_account_subtype,
        }
        return self.env["qbo.account.bridge.rule"].match_qbo_record(record)

    def _destination_is_locked(self, account):
        if getattr(account, "poseidon_kernel_locked", False):
            return True
        return bool(
            self.env["account.move.line"].search_count(
                [
                    ("account_id", "=", account.id),
                ],
                limit=1,
            ),
        )

    def _company_has_locked_period(self, company):
        return bool(company and (company.fiscalyear_lock_date or company.tax_lock_date))

    def _company_lock_message(self, company):
        return _(
            "The target company %(company)s has a locked accounting period. "
            "Unlock it before previewing or committing propagation."
        ) % {"company": company.display_name}

    def _bridge_rule_has_same_canonical_target(self, rule):
        self.ensure_one()
        decision_rule = self.decision_id.bridge_rule_id
        return (
            rule.canonical_code == decision_rule.canonical_code
            and rule.canonical_name == decision_rule.canonical_name
            and rule.canonical_account_type == decision_rule.canonical_account_type
        )


class PoseidonMappingPropagationLine(models.Model):
    _name = "poseidon.mapping.propagation.line"
    _description = "Poseidon Mapping Propagation Line"
    _order = "propagation_id, company_id, id"

    propagation_id = fields.Many2one(
        "poseidon.mapping.propagation",
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
    status = fields.Selection(
        [
            ("pending", "Pending"),
            ("applied", "Applied"),
            ("skipped", "Skipped"),
            ("conflict", "Conflict"),
            ("error", "Error"),
        ],
        required=True,
        default="pending",
        index=True,
    )
    source_account_id = fields.Many2one("account.account", string="Target QBO account", ondelete="set null")
    target_account_id = fields.Many2one("account.account", string="Canonical destination", ondelete="set null")
    existing_decision_id = fields.Many2one("poseidon.mapping.decision", ondelete="set null")
    existing_rule_id = fields.Many2one("qbo.account.bridge.rule", string="Existing rule", ondelete="set null")
    new_rule_id = fields.Many2one("qbo.account.bridge.rule", string="Rule to apply", ondelete="set null")
    qbo_id = fields.Char(index=True)
    qbo_account_name = fields.Char()
    message = fields.Text()
