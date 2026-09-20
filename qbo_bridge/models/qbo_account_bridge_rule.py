from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

ACCOUNT_TYPE_SELECTION = [
    ("asset_receivable", "Receivable"),
    ("asset_cash", "Bank and Cash"),
    ("asset_current", "Current Assets"),
    ("asset_non_current", "Non-current Assets"),
    ("asset_prepayments", "Prepayments"),
    ("asset_fixed", "Fixed Assets"),
    ("liability_payable", "Payable"),
    ("liability_credit_card", "Credit Card"),
    ("liability_current", "Current Liabilities"),
    ("liability_non_current", "Non-current Liabilities"),
    ("equity", "Equity"),
    ("equity_unaffected", "Current Year Earnings"),
    ("income", "Income"),
    ("income_other", "Other Income"),
    ("expense", "Expenses"),
    ("expense_other", "Other Expenses"),
    ("expense_depreciation", "Depreciation"),
    ("expense_direct_cost", "Cost of Revenue"),
    ("off_balance", "Off-Balance Sheet"),
]

# Didactic labels per canonical account type — plain language (pt/en), the
# normal balance, the statement section, and the coarse category. Used by the
# mapping coach (Fase 3) to explain a suggestion without accounting jargon.
ACCOUNT_TYPE_GUIDE = {
    "asset_receivable": {
        "normal_balance": "debit",
        "category": "asset",
        "pt": {"type_label": "Contas a receber", "statement": "Balanço patrimonial"},
        "en": {"type_label": "Accounts receivable", "statement": "Balance sheet"},
    },
    "asset_cash": {
        "normal_balance": "debit",
        "category": "asset",
        "pt": {"type_label": "Dinheiro e bancos", "statement": "Balanço patrimonial"},
        "en": {"type_label": "Cash and banks", "statement": "Balance sheet"},
    },
    "asset_current": {
        "normal_balance": "debit",
        "category": "asset",
        "pt": {"type_label": "Ativo circulante", "statement": "Balanço patrimonial"},
        "en": {"type_label": "Current assets", "statement": "Balance sheet"},
    },
    "asset_non_current": {
        "normal_balance": "debit",
        "category": "asset",
        "pt": {"type_label": "Ativo não circulante", "statement": "Balanço patrimonial"},
        "en": {"type_label": "Non-current assets", "statement": "Balance sheet"},
    },
    "asset_prepayments": {
        "normal_balance": "debit",
        "category": "asset",
        "pt": {"type_label": "Adiantamentos", "statement": "Balanço patrimonial"},
        "en": {"type_label": "Prepayments", "statement": "Balance sheet"},
    },
    "asset_fixed": {
        "normal_balance": "debit",
        "category": "asset",
        "pt": {"type_label": "Ativo imobilizado", "statement": "Balanço patrimonial"},
        "en": {"type_label": "Fixed assets", "statement": "Balance sheet"},
    },
    "liability_payable": {
        "normal_balance": "credit",
        "category": "liability",
        "pt": {"type_label": "Contas a pagar", "statement": "Balanço patrimonial"},
        "en": {"type_label": "Accounts payable", "statement": "Balance sheet"},
    },
    "liability_credit_card": {
        "normal_balance": "credit",
        "category": "liability",
        "pt": {"type_label": "Cartões de crédito a pagar", "statement": "Balanço patrimonial"},
        "en": {"type_label": "Credit cards payable", "statement": "Balance sheet"},
    },
    "liability_current": {
        "normal_balance": "credit",
        "category": "liability",
        "pt": {"type_label": "Passivo circulante", "statement": "Balanço patrimonial"},
        "en": {"type_label": "Current liabilities", "statement": "Balance sheet"},
    },
    "liability_non_current": {
        "normal_balance": "credit",
        "category": "liability",
        "pt": {"type_label": "Passivo não circulante", "statement": "Balanço patrimonial"},
        "en": {"type_label": "Non-current liabilities", "statement": "Balance sheet"},
    },
    "equity": {
        "normal_balance": "credit",
        "category": "equity",
        "pt": {"type_label": "Patrimônio líquido", "statement": "Balanço patrimonial"},
        "en": {"type_label": "Equity", "statement": "Balance sheet"},
    },
    "equity_unaffected": {
        "normal_balance": "credit",
        "category": "equity",
        "pt": {"type_label": "Lucros acumulados do exercício", "statement": "Balanço patrimonial"},
        "en": {"type_label": "Current year earnings", "statement": "Balance sheet"},
    },
    "income": {
        "normal_balance": "credit",
        "category": "income",
        "pt": {"type_label": "Receitas de vendas", "statement": "Demonstração de resultados"},
        "en": {"type_label": "Sales income", "statement": "Income statement"},
    },
    "income_other": {
        "normal_balance": "credit",
        "category": "income",
        "pt": {"type_label": "Outras receitas", "statement": "Demonstração de resultados"},
        "en": {"type_label": "Other income", "statement": "Income statement"},
    },
    "expense": {
        "normal_balance": "debit",
        "category": "expense",
        "pt": {"type_label": "Despesas operacionais", "statement": "Demonstração de resultados"},
        "en": {"type_label": "Operating expenses", "statement": "Income statement"},
    },
    "expense_other": {
        "normal_balance": "debit",
        "category": "expense",
        "pt": {"type_label": "Outras despesas", "statement": "Demonstração de resultados"},
        "en": {"type_label": "Other expenses", "statement": "Income statement"},
    },
    "expense_depreciation": {
        "normal_balance": "debit",
        "category": "expense",
        "pt": {"type_label": "Depreciação", "statement": "Demonstração de resultados"},
        "en": {"type_label": "Depreciation", "statement": "Income statement"},
    },
    "expense_direct_cost": {
        "normal_balance": "debit",
        "category": "expense",
        "pt": {"type_label": "Custo dos produtos vendidos", "statement": "Demonstração de resultados"},
        "en": {"type_label": "Cost of goods sold", "statement": "Income statement"},
    },
    "off_balance": {
        "normal_balance": "",
        "category": "off_balance",
        "pt": {"type_label": "Contas de compensação", "statement": "Fora do balanço"},
        "en": {"type_label": "Off-balance sheet", "statement": "Off-balance sheet"},
    },
}

CATEGORY_LABELS = {
    "asset": {"pt": "Ativo", "en": "Asset"},
    "liability": {"pt": "Passivo", "en": "Liability"},
    "equity": {"pt": "Patrimônio líquido", "en": "Equity"},
    "income": {"pt": "Receita", "en": "Revenue"},
    "expense": {"pt": "Despesa", "en": "Expense"},
    "off_balance": {"pt": "Compensação", "en": "Off-balance"},
}

NORMAL_BALANCE_LABELS = {
    "debit": {"pt": "Débito", "en": "Debit"},
    "credit": {"pt": "Crédito", "en": "Credit"},
}


def account_type_guide_labels(account_type, lang="pt"):
    """Flattened didactic labels for one Odoo canonical account type.

    Returns an empty dict for unknown/blank types so callers can `or {}`.
    """
    raw = ACCOUNT_TYPE_GUIDE.get(account_type or "")
    if not raw:
        return {}
    lang = "pt" if lang == "pt" else "en"
    labels = raw[lang]
    normal_balance = raw["normal_balance"]
    return {
        "normal_balance": normal_balance,
        "normal_balance_label": NORMAL_BALANCE_LABELS.get(normal_balance, {}).get(lang) or False,
        "category": CATEGORY_LABELS.get(raw["category"], {}).get(lang) or raw["category"],
        "category_key": raw["category"],
        "type_label": labels["type_label"],
        "statement": labels["statement"],
    }


def mapping_match_reason(rule, record, lang="pt"):
    """Why a bridge rule matched a QBO record, in plain language.

    Returns False when there is no rule so the caller can fall back to the
    decision reason or "no suggestion yet".
    """
    if not rule:
        return False
    criteria = []
    if rule.match_acct_num and record.get("AcctNum"):
        criteria.append(_("número de conta %s") % record["AcctNum"])
    if rule.match_name and record.get("Name"):
        criteria.append(_("nome \"%s\"") % record["Name"])
    if rule.match_account_type and record.get("AccountType"):
        criteria.append(_("tipo %s") % record["AccountType"])
    if rule.match_account_subtype and record.get("AccountSubType"):
        criteria.append(_("subtipo %s") % record["AccountSubType"])
    if not criteria:
        return _("Regra de ponte genérica")
    return _("Regra de ponte casou por %s") % ", ".join(criteria)


class QboAccountBridgeRule(models.Model):
    _name = "qbo.account.bridge.rule"
    _description = "QBO canonical account bridge rule"
    _order = "sequence, canonical_code, id"

    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    name = fields.Char(
        compute="_compute_name",
        store=True,
    )

    match_acct_num = fields.Char(string="Match QBO account number")
    match_name = fields.Char(string="Match QBO account name")
    match_account_type = fields.Char(string="Match QBO account type")
    match_account_subtype = fields.Char(string="Match QBO detail type")

    canonical_code = fields.Char(string="Canonical code", required=True)
    canonical_name = fields.Char(string="Canonical name", required=True)
    canonical_account_type = fields.Selection(
        ACCOUNT_TYPE_SELECTION,
        string="Canonical account type",
        required=True,
    )
    notes = fields.Text()
    linked_account_count = fields.Integer(
        compute="_compute_linked_account_count",
        string="Linked accounts",
    )

    @api.depends("canonical_code", "canonical_name")
    def _compute_name(self):
        for rec in self:
            parts = [part for part in [rec.canonical_code, rec.canonical_name] if part]
            rec.name = " - ".join(parts)

    def _compute_linked_account_count(self):
        grouped = self.env["account.account"].read_group(
            [("qbo_bridge_rule_id", "in", self.ids)],
            ["qbo_bridge_rule_id"],
            ["qbo_bridge_rule_id"],
        )
        count_by_rule = {
            item["qbo_bridge_rule_id"][0]: item["qbo_bridge_rule_id_count"]
            for item in grouped
            if item.get("qbo_bridge_rule_id")
        }
        for rec in self:
            rec.linked_account_count = count_by_rule.get(rec.id, 0)

    @api.constrains(
        "match_acct_num",
        "match_name",
        "match_account_type",
        "match_account_subtype",
    )
    def _check_match_fields(self):
        for rec in self:
            if not any(
                [
                    rec.match_acct_num,
                    rec.match_name,
                    rec.match_account_type,
                    rec.match_account_subtype,
                ],
            ):
                raise ValidationError(
                    _("Add at least one QBO match field to the bridge rule."),
                )

    @api.model
    def match_qbo_record(self, record):
        for rule in self.search([("active", "=", True)]):
            if rule._matches_record(record):
                return rule
        return self.browse()

    def _matches_record(self, record):
        self.ensure_one()
        comparators = {
            "match_acct_num": record.get("AcctNum"),
            "match_name": record.get("Name"),
            "match_account_type": record.get("AccountType"),
            "match_account_subtype": record.get("AccountSubType"),
        }
        for field_name, value in comparators.items():
            rule_value = getattr(self, field_name)
            if rule_value and self._normalize(rule_value) != self._normalize(value):
                return False
        return True

    @api.model
    def _normalize(self, value):
        return (value or "").strip().casefold()

    def action_view_linked_accounts(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Accounts using %s") % self.display_name,
            "res_model": "account.account",
            "view_mode": "list,form",
            "domain": [("qbo_bridge_rule_id", "=", self.id)],
            "context": {"search_default_current_company": 1},
        }
