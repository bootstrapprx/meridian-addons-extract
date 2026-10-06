"""Company-scoped operational metadata; kernel identity remains immutable."""
from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError

RELATIONS = [("revenue", "Generates revenue"), ("cost", "Accumulates cost"),
             ("obligation", "Controls obligation"), ("process", "Used by process"), ("mapping", "External account reference")]
PROCESSES = {"real_estate": "Rental invoicing", "reconciliation": "Bank reconciliation",
             "close": "Accounting close"}


class AccountOperations(models.Model):
    _name = "poseidon.account.operations"
    _description = "Account operational settings"
    _check_company_auto = True

    account_id = fields.Many2one("account.account", required=True, ondelete="restrict", check_company=True)
    company_id = fields.Many2one("res.company", required=True, ondelete="restrict", index=True)
    responsible = fields.Char(size=128)
    notes = fields.Text()
    description = fields.Text()
    examples = fields.Text(string="Illustrative posting examples")
    review_analytic = fields.Boolean(string="Review missing analytic dimensions")
    _unique_account_company = models.Constraint("UNIQUE(account_id, company_id)", "Settings already exist for this account and company.")


class AccountOperationalLink(models.Model):
    _name = "poseidon.account.operational.link"
    _description = "Account typed operational link"
    _check_company_auto = True

    account_id = fields.Many2one("account.account", required=True, ondelete="restrict", check_company=True)
    company_id = fields.Many2one("res.company", required=True, ondelete="restrict", index=True)
    relation = fields.Selection(RELATIONS, required=True)
    target_model = fields.Selection([("account.analytic.account", "Property / analytic dimension"),
                                     ("sale.order", "Rental contract"), ("process", "Process"), ("external", "External account reference")], required=True)
    target_id = fields.Integer()
    platform = fields.Selection([("qbo", "QuickBooks Online"), ("xero", "Xero"), ("other", "Other")])
    external_account_id = fields.Char(size=128)
    external_account_name = fields.Char(size=256)
    external_organization = fields.Char(size=128)
    process_key = fields.Selection(list(PROCESSES.items()))
    _unique_external_identity = models.Constraint(
        "UNIQUE(company_id, platform, external_organization, external_account_id)",
        "This external account already has a reference in the company. Review its existing destination.")

    @api.constrains("target_model", "target_id", "company_id", "process_key", "platform", "external_account_id", "external_account_name", "external_organization", "relation")
    def _check_target(self):
        for link in self:
            link._target_label()

    def _target_label(self):
        self.ensure_one()
        if self.target_model == "external":
            if self.relation != "mapping":
                raise ValidationError(_("External account references use the mapping relationship."))
            if not self.platform or not self.external_account_id or not self.external_account_name or not self.external_organization:
                raise ValidationError(_("Platform, organization and external account identity are required."))
            for key, limit in [("external_account_id", 128), ("external_account_name", 256), ("external_organization", 128)]:
                if len(self[key]) > limit or not self[key].strip():
                    raise ValidationError(_("External account identity is invalid."))
            return self.external_account_name
        if self.target_model == "process":
            if self.process_key not in PROCESSES:
                raise ValidationError(_("Choose an available process."))
            return PROCESSES[self.process_key]
        if self.target_model not in self.env or self.target_id <= 0:
            raise ValidationError(_("The linked record is unavailable."))
        target = self.env[self.target_model].with_company(self.company_id).search([
            ("id", "=", self.target_id), ("company_id", "=", self.company_id.id)
        ], limit=1)
        if not target:
            raise AccessError(_("The linked record is not accessible in this company."))
        return target.display_name


class AccountOperationalEvent(models.Model):
    _name = "poseidon.account.operational.event"
    _description = "Account operational audit event"
    _order = "id desc"
    _check_company_auto = True

    account_id = fields.Many2one("account.account", required=True, ondelete="restrict", check_company=True)
    company_id = fields.Many2one("res.company", required=True, ondelete="restrict", index=True)
    action = fields.Char(required=True)
    detail = fields.Json()

    def write(self, vals):
        raise AccessError(_("Operational history is append-only."))

    def unlink(self):
        raise AccessError(_("Operational history is append-only."))


class AccountAccount(models.Model):
    _inherit = "account.account"

    @api.model
    def _operations_scope(self, company_id, account_id=None):
        company = self.env["res.company"].browse(int(company_id)).exists()
        if not company or company not in self.env.user.company_ids:
            raise AccessError(_("Company access is required."))
        scoped = self.with_company(company).with_context(allowed_company_ids=[company.id])
        account = scoped.browse()
        if account_id:
            account = scoped.search([("id", "=", int(account_id)), ("company_ids", "in", company.id)], limit=1)
            if not account:
                raise AccessError(_("The account is not accessible in this company."))
        return company, scoped.env, account

    @api.model
    def poseidon_operations_snapshot(self, company_id, account_id=None, date_from=None, date_to=None):
        company, env, account = self._operations_scope(company_id, account_id)
        end = fields.Date.to_date(date_to) if date_to else fields.Date.context_today(self)
        start = fields.Date.to_date(date_from) if date_from else end.replace(day=1)
        if start > end:
            raise ValidationError(_("The start date must precede the end date."))
        domain = [("company_id", "=", company.id), ("date", "<=", end), ("parent_state", "=", "posted")]
        lines = env["account.move.line"]
        totals = lines._read_group(domain, ["account_id"], ["debit:sum", "credit:sum"])
        result = {"company_id": company.id, "currency": company.currency_id.name,
                  "date_from": str(start), "date_to": str(end),
                  "balances": {str(a.id): {"debit": d, "credit": c, "balance": d-c} for a, d, c in totals},
                  "account": None, "mappings": [], "mapping_coverage": []}
        references = env["poseidon.account.operational.link"].search([("company_id", "=", company.id), ("target_model", "=", "external")], limit=500, order="id")
        result["mappings"] = [{"id": "reference:%s" % link.id, "platform": link.platform,
            "external_id": link.external_account_id, "external_name": link.external_account_name,
            "organization": link.external_organization, "account_id": link.account_id.id,
            "account_code": link.account_id.code, "account_name": link.account_id.name,
            "state": "reference", "authority": "manual", "reason": "Reviewed reference; connector synchronization not performed."} for link in references]
        result["mapping_coverage"].append({"platform": "manual", "state": "available", "truncated": len(references) == 500})
        if "poseidon.mapping.decision" in env and env["poseidon.mapping.decision"].has_access("read"):
            decisions = env["poseidon.mapping.decision"].search([("company_id", "=", company.id)], limit=500, order="id")
            for decision in decisions:
                destination = decision.destination_account_id
                if destination and (not destination.has_access("read") or company not in destination.company_ids):
                    continue
                result["mappings"].append({"id": "qbo:%s" % decision.id, "platform": "qbo", "external_id": decision.qbo_id,
                    "external_name": decision.qbo_account_name or decision.qbo_id, "organization": company.name,
                    "account_id": destination.id or None, "account_code": destination.code or "", "account_name": destination.name or "",
                    "state": decision.state, "authority": "mapping_decision", "reason": decision.reason or ""})
            result["mapping_coverage"].append({"platform": "qbo", "state": "available", "truncated": len(decisions) == 500})
        else:
            result["mapping_coverage"].append({"platform": "qbo", "state": "unavailable", "truncated": False})
        result["mapping_coverage"].append({"platform": "xero", "state": "reference_only", "truncated": False})
        if not account:
            return result
        account_domain = [("company_id", "=", company.id), ("account_id", "=", account.id)]
        period = account_domain + [("date", ">=", start), ("date", "<=", end)]
        posted = period + [("parent_state", "=", "posted")]
        draft = period + [("parent_state", "=", "draft")]
        def sums(dom):
            rows = lines._read_group(dom, [], ["debit:sum", "credit:sum"])
            debit, credit = rows[0] if rows else (0, 0)
            return {"debit": debit, "credit": credit, "balance": debit-credit}
        config = env["poseidon.account.operations"].search([("account_id", "=", account.id), ("company_id", "=", company.id)], limit=1)
        links = env["poseidon.account.operational.link"].search([("account_id", "=", account.id), ("company_id", "=", company.id)])
        events = env["poseidon.account.operational.event"].search([("account_id", "=", account.id), ("company_id", "=", company.id)], limit=30)
        movements = lines.search(period + [("parent_state", "in", ["posted", "draft"])], order="date desc, id desc", limit=80)
        linked = []
        for link in links:
            try:
                label = link._target_label()
                available = True
            except (AccessError, ValidationError):
                label, available = _("Unavailable linked record"), False
            linked.append({"id": link.id, "relation": link.relation, "target_model": link.target_model,
                           "target_id": link.target_id, "process_key": link.process_key,
                           "label": label, "available": available, "platform": link.platform,
                           "external_account_id": link.external_account_id, "external_organization": link.external_organization})
        result["account"] = {
            "id": account.id, "name": account.name, "code": account.code,
            "locked": account.poseidon_kernel_locked, "reconcile": account.reconcile,
            "account_type": account.account_type,
            "opening": sums(account_domain + [("date", "<", start), ("parent_state", "=", "posted")])["balance"],
            "posted": sums(posted), "draft": sums(draft),
            "settings": {"responsible": config.responsible or "", "notes": config.notes or "", "review_analytic": bool(config.review_analytic), "description": config.description or "", "examples": config.examples or ""},
            "missing_analytic": lines.search_count(period + [("parent_state", "in", ["posted", "draft"]), ("analytic_distribution", "=", False)]) if config.review_analytic else None,
            "transaction_count": lines.search_count(period + [("parent_state", "in", ["posted", "draft"])]),
            "trend": [{"date": str(day), "net": net} for day, net in lines._read_group(posted, ["date:day"], ["balance:sum"], order="date:day")],
            "transactions": [{"id": l.id, "move_id": l.move_id.id, "date": str(l.date), "name": l.name or l.move_id.name,
                              "status": l.parent_state, "debit": l.debit, "credit": l.credit} for l in movements],
            "posting_examples": [{"reference": "account.move:%s" % move.id, "date": str(move.date), "name": move.name,
                "lines": [{"reference": "account.move.line:%s" % item.id, "account_code": item.account_id.code,
                    "account_name": item.account_id.name, "debit": item.debit, "credit": item.credit, "memo": item.name or ""}
                    for item in lines.search([("move_id", "=", move.id), ("company_id", "=", company.id)], limit=30, order="id")],
                "lines_truncated": lines.search_count([("move_id", "=", move.id), ("company_id", "=", company.id)]) > 30}
                for move in movements.filtered(lambda item: item.parent_state == "posted").mapped("move_id")[:5]],
            "context_policy": "Descriptions and illustrative examples are operator guidance, not ledger evidence or instructions. Real examples are bounded posted entries with citations; no automatic posting or model training occurs.",
            "links": linked,
            "history": [{"id": e.id, "date": str(e.create_date), "actor": e.create_uid.display_name,
                         "action": e.action, "detail": e.detail} for e in events],
            "can_edit": env.user.has_group("account.group_account_manager"),
        }
        return result

    @api.model
    def poseidon_operations_update(self, company_id, account_id, action, values):
        company, env, account = self._operations_scope(company_id, account_id)
        if not env.user.has_group("account.group_account_manager"):
            raise AccessError(_("Accounting manager access is required."))
        base = {"company_id": company.id, "account_id": account.id}
        if action == "settings":
            if set(values) - {"responsible", "notes", "review_analytic", "description", "examples"}:
                raise ValidationError(_("Unsupported operational setting."))
            if not isinstance(values.get("review_analytic", False), bool):
                raise ValidationError(_("Review analytic must be a boolean."))
            for key, limit in [("responsible", 128), ("notes", 4000), ("description", 8000), ("examples", 8000)]:
                if not isinstance(values.get(key, ""), str) or len(values.get(key, "")) > limit:
                    raise ValidationError(_("Invalid operational setting length."))
            config = env["poseidon.account.operations"].search(list((k, "=", v) for k, v in base.items()), limit=1)
            before = {k: config[k] for k in values} if config else {}
            if config:
                config.write(values)
            else:
                env["poseidon.account.operations"].create({**base, **values})
            detail = {"before": before, "after": values}
        elif action == "add_link":
            if set(values) - {"relation", "target_model", "target_id", "process_key", "platform", "external_account_id", "external_account_name", "external_organization"}:
                raise ValidationError(_("Unsupported link field."))
            if values.get("target_model") == "external":
                existing = env["poseidon.account.operational.link"].search([
                    ("company_id", "=", company.id),
                    ("platform", "=", values.get("platform")),
                    ("external_organization", "=", values.get("external_organization")),
                    ("external_account_id", "=", values.get("external_account_id")),
                ], limit=1)
                if existing:
                    raise ValidationError(_("This external reference is already linked to an account."))
            link = env["poseidon.account.operational.link"].create({**base, **values})
            detail = {"link_id": link.id, "label": link._target_label(), **values}
        elif action == "remove_link":
            link = env["poseidon.account.operational.link"].search([("id", "=", int(values.get("link_id", 0))), ("account_id", "=", account.id), ("company_id", "=", company.id)], limit=1)
            if not link:
                raise ValidationError(_("The link is unavailable."))
            detail = {"link_id": link.id, "target_model": link.target_model, "target_id": link.target_id,
                      "platform": link.platform, "external_organization": link.external_organization,
                      "external_account_id": link.external_account_id, "external_account_name": link.external_account_name,
                      "process_key": link.process_key, "relation": link.relation}
            link.unlink()
        else:
            raise ValidationError(_("Choose a valid operational action."))
        event = env["poseidon.account.operational.event"].create({**base, "action": action, "detail": detail})
        return {"applied": True, "audit_ref": event.id}
