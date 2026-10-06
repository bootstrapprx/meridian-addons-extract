"""Explicit source tax translation; rates remain native accounting data."""
from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError


class QboTaxMapping(models.Model):
    _name = "qbo.tax.mapping"
    _description = "QuickBooks tax translation"
    _order = "source_tax_code, direction, id"

    company_id = fields.Many2one("res.company", required=True, index=True)
    realm_id = fields.Many2one("qbo.realm", required=True, ondelete="restrict")
    source_tax_code = fields.Char(required=True, index=True)
    direction = fields.Selection([("sale", "Sale"), ("purchase", "Purchase")], required=True)
    tax_ids = fields.Many2many("account.tax", string="Canonical taxes", check_company=True)
    active = fields.Boolean(default=True)
    _unique_source = models.Constraint("UNIQUE(company_id, realm_id, source_tax_code, direction)",
                                       "A source tax reference must have one translation per company and connection.")

    @api.constrains("company_id", "realm_id", "source_tax_code", "direction", "tax_ids", "active")
    def _check_translation(self):
        for record in self:
            if not record.source_tax_code or not record.source_tax_code.strip() or record.source_tax_code != record.source_tax_code.strip() or len(record.source_tax_code) > 255:
                raise ValidationError("A source tax code is required.")
            if not self.env["qbo.company.mapping"].search_count([
                ("company_id", "=", record.company_id.id), ("realm_id", "=", record.realm_id.id),
            ]):
                raise ValidationError("The connection does not belong to this company.")
            for tax in record.tax_ids:
                if tax.company_id != record.company_id or tax.type_tax_use != record.direction or not tax.active:
                    raise ValidationError("Select active taxes from this company and document direction.")

    @api.model
    def _company_mapping(self, company_id):
        company = self.env["res.company"].browse(int(company_id)).exists()
        company.check_access("read")
        if len(company) != 1 or company not in self.env.user.company_ids:
            raise UserError("Select an authorized company.")
        mappings = self.env["qbo.company.mapping"].search([("company_id", "=", company.id)], limit=2)
        if len(mappings) != 1:
            raise UserError("Review the company connection before translating taxes.")
        return company, mappings.realm_id

    @api.model
    def review_configuration(self, company_id):
        company, realm = self._company_mapping(company_id)
        mappings = self.with_context(active_test=False).search([
            ("company_id", "=", company.id), ("realm_id", "=", realm.id)])
        taxes = self.env["account.tax"].search([("company_id", "=", company.id),
            ("active", "=", True), ("type_tax_use", "in", ["sale", "purchase"])])
        moves = self.env["account.move"].search([("company_id", "=", company.id),
            "|", ("qbo_realm_id", "=", realm.id), ("qbo_realm_id", "=", False), ("qbo_id", "!=", False), ("state", "=", "draft"), ("move_type", "in", ["out_invoice", "in_invoice"])], limit=100)
        payments = self.env["account.payment"].search([("company_id", "=", company.id),
            "|", ("qbo_realm_id", "=", realm.id), ("qbo_realm_id", "=", False), ("qbo_id", "!=", False), ("state", "=", "draft"), ("qbo_link_state", "=", "review")], limit=100)
        move_reasons = {move.id: move._qbo_current_conversion_reasons() for move in moves}
        return {"company_id": company.id, "realm_id": realm.id, "can_configure": self.env.user.has_group("qbo_bridge.group_qbo_bridge_manager"),
            "mappings": [{"id": row.id, "source_tax_code": row.source_tax_code, "direction": row.direction,
                "tax_ids": row.tax_ids.ids, "active": row.active} for row in mappings],
            "taxes": [{"id": tax.id, "name": tax.name, "direction": tax.type_tax_use} for tax in taxes],
            "exceptions": [{"id": move.id, "kind": "document", "source_id": move.qbo_id,
                "reference": move.display_name, "reasons": move_reasons[move.id]} for move in moves if move_reasons[move.id]] +
                [{"id": payment.id, "kind": "payment", "source_id": payment.qbo_id,
                    "reference": payment.display_name, "reasons": payment.qbo_link_reasons or (["Legacy source connection requires review."] if not payment.qbo_realm_id else [])} for payment in payments],
            "exception_limit_per_type": 100}

    @api.model
    def apply_configuration(self, company_id, proposals, human_confirmed=False):
        if human_confirmed is not True:
            raise UserError("Review and confirm tax translations.")
        if not self.env.user.has_group("qbo_bridge.group_qbo_bridge_manager"):
            raise UserError("Connection manager permission is required to change tax translations.")
        company, realm = self._company_mapping(company_id)
        if not isinstance(proposals, list) or not proposals or len(proposals) > 100:
            raise UserError("Submit between 1 and 100 reviewed translations.")
        with self.env.cr.savepoint():
            for proposal in proposals:
                if not isinstance(proposal, dict) or set(proposal) != {"source_tax_code", "direction", "tax_ids", "active"}:
                    raise UserError("Invalid translation proposal.")
                code = proposal["source_tax_code"]
                ids = proposal["tax_ids"]
                if not isinstance(code, str) or not code.strip() or len(code) > 255 or proposal["direction"] not in ("sale", "purchase") or type(proposal["active"]) is not bool:
                    raise UserError("Invalid source tax reference or direction.")
                if not isinstance(ids, list) or any(type(value) is not int or value <= 0 for value in ids):
                    raise UserError("Select valid canonical taxes.")
                taxes = self.env["account.tax"].browse(ids).exists()
                taxes.check_access("read")
                if set(taxes.ids) != set(ids):
                    raise UserError("One or more selected taxes are unavailable.")
                domain = [("company_id", "=", company.id), ("realm_id", "=", realm.id),
                          ("source_tax_code", "=", code.strip()), ("direction", "=", proposal["direction"])]
                row = self.with_context(active_test=False).search(domain, limit=1)
                values = {"company_id": company.id, "realm_id": realm.id, "source_tax_code": code.strip(),
                    "direction": proposal["direction"], "active": proposal["active"], "tax_ids": [fields.Command.set(ids)]}
                if row:
                    row.write(values)
                else:
                    self.create(values)
            self.env["qbo.sync.log"].log(self.env,
                self.env["qbo.company.mapping"].search([("company_id", "=", company.id), ("realm_id", "=", realm.id)], limit=1),
                "mapping", "pull", "success", "update", message="Reviewed tax translations saved; no import or accounting action executed.")
        return self.review_configuration(company.id)
