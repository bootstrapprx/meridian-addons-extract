"""QboMixin — adds QBO tracking fields to native Odoo models.

These fields are declared on the mixin so each target model only needs
``_inherit = ['<native.model>', 'qbo.mixin']`` to gain QBO tracking.
"""
from odoo import fields, models


class QboMixin(models.AbstractModel):
    """Abstract mixin that adds QBO identity and sync-state fields."""

    _name = "qbo.mixin"
    _description = "QBO sync mixin"

    qbo_id = fields.Char(
        string="QBO ID",
        index=True,
        copy=False,
        help="The entity Id returned by the QuickBooks Online API.",
    )
    qbo_source_type = fields.Char(index=True, copy=False)

    qbo_sync_token = fields.Char(
        string="QBO SyncToken",
        copy=False,
        help="QBO optimistic-lock token; must be sent with every update.",
    )
    qbo_realm_id = fields.Many2one(
        "qbo.realm",
        string="QBO realm",
        ondelete="set null",
        copy=False,
        help="The realm this record was last synced from/to.",
    )
    qbo_last_sync = fields.Datetime(
        string="Last QBO sync",
        readonly=True,
        copy=False,
    )


# ── Apply mixin to bridged models ────────────────────────────────────────────

class AccountAccountQbo(models.Model):
    _name = "account.account"
    _inherit = ["account.account", "qbo.mixin"]
    _description = "Account (QBO bridge)"


class ResPartnerQbo(models.Model):
    _name = "res.partner"
    _inherit = ["res.partner", "qbo.mixin"]
    _description = "Partner (QBO bridge)"


class AccountMoveQbo(models.Model):
    _name = "account.move"
    _inherit = ["account.move", "qbo.mixin"]
    _description = "Journal entry / invoice (QBO bridge)"


class AccountPaymentQbo(models.Model):
    _name = "account.payment"
    _inherit = ["account.payment", "qbo.mixin"]
    _description = "Payment (QBO bridge)"


class ProductTemplateQbo(models.Model):
    _name = "product.template"
    _inherit = ["product.template", "qbo.mixin"]
    _description = "Product (QBO bridge)"


class AccountMoveQboConversion(models.Model):
    _inherit = "account.move"

    _qbo_source_unique = models.Constraint("UNIQUE(company_id, qbo_realm_id, qbo_source_type, qbo_id)", "A source document already exists in this company and connection.")
    qbo_conversion_evidence = fields.Json(copy=False)
    qbo_conversion_state = fields.Selection([("review", "Needs review"), ("validated", "Validated")], default="review", copy=False)
    qbo_conversion_reasons = fields.Json(copy=False)

    def _qbo_tax_signature(self, taxes):
        pending = list(taxes)
        seen = {}
        while pending:
            tax = pending.pop()
            if tax.id in seen:
                continue
            seen[tax.id] = {"id": tax.id, "company": tax.company_id.id,
                "direction": tax.type_tax_use, "active": tax.active,
                "amount": tax.amount, "amount_type": tax.amount_type,
                "price_include": tax.price_include, "sequence": tax.sequence,
                "include_base": tax.include_base_amount, "base_affected": tax.is_base_affected,
                "exigibility": tax.tax_exigibility,
                "children": sorted(tax.children_tax_ids.ids)}
            pending.extend(tax.children_tax_ids)
        return [seen[key] for key in sorted(seen)]

    def _qbo_current_conversion_reasons(self):
        self.ensure_one()
        evidence = self.qbo_conversion_evidence or {}
        reasons = list(evidence.get("reasons") or [])
        if not evidence.get("total_present") or not evidence.get("tax_present"):
            reasons.append("Source total and tax evidence are required.")
        if evidence.get("currency") != self.currency_id.name:
            reasons.append("Source currency is missing or differs from the document.")
        if not self.qbo_realm_id or not self.qbo_source_type:
            reasons.append("Source connection and document identity require review.")
        direction = "sale" if self.move_type == "out_invoice" else "purchase"
        codes = evidence.get("tax_codes") or []
        signatures = evidence.get("tax_configuration")
        if not isinstance(signatures, list):
            reasons.append("Canonical tax configuration evidence requires review.")
        lines = self.invoice_line_ids.filtered(lambda line: line.display_type not in ("line_section", "line_note"))
        if len(codes) != len(lines):
            reasons.append("Source and destination line evidence differs.")
        for index, (line, code) in enumerate(zip(lines, codes)):
            mapping = self.env["qbo.tax.mapping"].search([
                ("company_id", "=", self.company_id.id), ("realm_id", "=", self.qbo_realm_id.id),
                ("source_tax_code", "=", code or ""), ("direction", "=", direction)], limit=2)
            if not isinstance(signatures, list) or index >= len(signatures) or signatures[index] != self._qbo_tax_signature(mapping.tax_ids):
                reasons.append("Canonical tax configuration changed after translation.")
            if len(mapping) != 1 or any(not tax.active or tax.company_id != self.company_id or tax.type_tax_use != direction for tax in mapping.tax_ids) or set(line.tax_ids.ids) != set(mapping.tax_ids.ids):
                reasons.append("Source tax translation is missing, changed or inactive: %s." % (code or "unavailable"))
        if evidence.get("total_present") and self.currency_id.compare_amounts(self.amount_total, evidence["total"]) != 0:
            reasons.append("Converted document total differs from QuickBooks.")
        if evidence.get("tax_present") and self.currency_id.compare_amounts(self.amount_tax, evidence["tax"]) != 0:
            reasons.append("Converted tax amount differs from QuickBooks.")
        return list(dict.fromkeys(reasons))

    def _qbo_validate_conversion(self):
        for move in self:
            if move.state == "posted" or not move.qbo_id or move.move_type not in ("out_invoice", "in_invoice"):
                continue
            reasons = move._qbo_current_conversion_reasons()
            move.qbo_conversion_reasons = reasons
            move.qbo_conversion_state = "review" if reasons else "validated"
        return self

    def _post(self, soft=True):
        imported = self.filtered(lambda move: move.state == "draft" and move.qbo_id and move.move_type in ("out_invoice", "in_invoice"))
        imported._qbo_validate_conversion()
        if any(move.qbo_conversion_state != "validated" for move in imported):
            from odoo.exceptions import UserError
            raise UserError("Review source tax translation, currency and totals before posting QuickBooks documents.")
        return super()._post(soft=soft)


class AccountPaymentQboConversion(models.Model):
    _inherit = "account.payment"

    _qbo_source_unique = models.Constraint("UNIQUE(company_id, qbo_realm_id, qbo_source_type, qbo_id)", "A source payment already exists in this company and connection.")
    qbo_link_evidence = fields.Json(copy=False)
    qbo_link_reasons = fields.Json(copy=False)
    qbo_linked_move_ids = fields.Many2many("account.move", "qbo_payment_source_move_rel", "payment_id", "move_id", copy=False, check_company=True)
    qbo_unapplied_amount = fields.Monetary(currency_field="currency_id", copy=False)
    qbo_link_state = fields.Selection([("review", "Needs review"), ("validated", "Validated")], default="review", copy=False)
