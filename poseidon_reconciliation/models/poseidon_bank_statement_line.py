import hashlib
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class PoseidonBankStatementLine(models.Model):
    _name = "poseidon.bank.statement.line"
    _description = "Poseidon Bank Statement Line"
    _order = "date desc, id desc"

    name = fields.Char(compute="_compute_name", store=True)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
        ondelete="cascade",
    )
    currency_id = fields.Many2one(
        "res.currency",
        related="company_id.currency_id",
        readonly=True,
        store=True,
    )
    date = fields.Date(required=True, index=True)
    amount = fields.Monetary(required=True, currency_field="currency_id")
    description = fields.Char(required=True)
    description_key = fields.Char(index=True, copy=False)
    source = fields.Selection(
        [
            ("csv", "CSV"),
            ("ofx", "OFX"),
            ("manual", "Manual"),
        ],
        required=True,
        default="csv",
        index=True,
    )
    source_file = fields.Char(copy=False)
    source_row = fields.Integer(copy=False)
    external_id = fields.Char(copy=False, index=True)
    import_hash = fields.Char(copy=False, index=True)
    status = fields.Selection(
        [
            ("unmatched", "Unmatched"),
            ("suggested", "Suggested"),
            ("confirmed", "Confirmed"),
            ("ignored", "Ignored"),
        ],
        required=True,
        default="unmatched",
        index=True,
    )
    bank_account_id = fields.Many2one(
        "account.account",
        string="Bank account",
        required=True,
        index=True,
        ondelete="restrict",
    )
    counterpart_account_id = fields.Many2one(
        "account.account",
        string="Counterpart account",
        index=True,
        ondelete="restrict",
    )
    suggestion_ids = fields.One2many(
        "poseidon.reconciliation.suggestion",
        "statement_line_id",
        string="Suggestions",
    )
    move_id = fields.Many2one("account.move", string="Confirmed journal entry", readonly=True, copy=False)
    move_line_id = fields.Many2one("account.move.line", string="Bank journal item", readonly=True, copy=False)
    confirmed_at = fields.Datetime(readonly=True, copy=False)
    confirmed_by_id = fields.Many2one("res.users", readonly=True, copy=False, ondelete="set null")
    ignored_at = fields.Datetime(readonly=True, copy=False)
    ignored_by_id = fields.Many2one("res.users", readonly=True, copy=False, ondelete="set null")
    ignore_reason = fields.Text(copy=False)
    note = fields.Text()

    _amount_nonzero = models.Constraint(
        "CHECK(amount != 0)",
        "Bank statement line amount cannot be zero.",
    )

    @api.depends("date", "amount", "description")
    def _compute_name(self):
        for line in self:
            line.name = "%s %s %s" % (
                line.date or "",
                line.amount or 0,
                line.description or _("Bank statement line"),
            )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._prepare_normalized_vals(vals)
        return super().create(vals_list)

    def write(self, vals):
        vals = dict(vals)
        if {"description", "date", "amount", "external_id", "source_file", "source_row", "company_id"} & vals.keys():
            for line in self:
                prepared = {
                    "description": vals.get("description", line.description),
                    "date": vals.get("date", line.date),
                    "amount": vals.get("amount", line.amount),
                    "external_id": vals.get("external_id", line.external_id),
                    "source_file": vals.get("source_file", line.source_file),
                    "source_row": vals.get("source_row", line.source_row),
                    "company_id": vals.get("company_id", line.company_id.id),
                }
                self._prepare_normalized_vals(prepared)
                vals.setdefault("description_key", prepared.get("description_key"))
                vals.setdefault("import_hash", prepared.get("import_hash"))
                break
        return super().write(vals)

    @api.constrains("company_id", "bank_account_id", "counterpart_account_id")
    def _check_account_company_scope(self):
        for line in self:
            line._assert_account_in_company(line.bank_account_id)
            if line.counterpart_account_id:
                line._assert_account_in_company(line.counterpart_account_id)

    def action_ignore(self, reason=False):
        for line in self:
            if line.status == "confirmed":
                raise UserError(_("Confirmed statement lines cannot be ignored. Delete the linked draft journal entry in kodoo, or reverse it if it has already been posted."))
            line.write(
                {
                    "status": "ignored",
                    "ignore_reason": reason or _("Ignored in Poseidon reconciliation."),
                    "ignored_at": fields.Datetime.now(),
                    "ignored_by_id": self.env.user.id,
                },
            )
        return True

    def confirm_with_counterpart(self, counterpart_account_id, label=False):
        self.ensure_one()
        if self.status == "confirmed":
            raise UserError(_("This statement line is already confirmed."))
        if self.status == "ignored":
            raise UserError(_("Ignored statement lines cannot be confirmed."))

        counterpart = self.env["account.account"].browse(counterpart_account_id).exists()
        if not counterpart:
            raise UserError(_("A counterpart account is required."))

        self._assert_date_writable()
        self._assert_account_writable(self.bank_account_id)
        self._assert_account_writable(counterpart)

        journal = self._find_general_journal()
        amount = abs(self.amount)
        description = label or self.description
        if self.amount > 0:
            bank_debit = amount
            bank_credit = 0.0
            counterpart_debit = 0.0
            counterpart_credit = amount
        else:
            bank_debit = 0.0
            bank_credit = amount
            counterpart_debit = amount
            counterpart_credit = 0.0

        move = self.env["account.move"].with_company(self.company_id).create(
            {
                "move_type": "entry",
                "journal_id": journal.id,
                "company_id": self.company_id.id,
                "date": self.date,
                "ref": _("Bank reconciliation: %s") % self.description,
                "line_ids": [
                    (
                        0,
                        0,
                        {
                            "account_id": self.bank_account_id.id,
                            "name": description,
                            "debit": bank_debit,
                            "credit": bank_credit,
                        },
                    ),
                    (
                        0,
                        0,
                        {
                            "account_id": counterpart.id,
                            "name": description,
                            "debit": counterpart_debit,
                            "credit": counterpart_credit,
                        },
                    ),
                ],
            },
        )
        # Poseidon stages reconciliation entries as DRAFT account.move records.
        # Posting to the general ledger is a separate, human-reviewed step in
        # Odoo (maker-checker) — do NOT call move.action_post() here.
        bank_line = move.line_ids.filtered(lambda move_line: move_line.account_id == self.bank_account_id)[:1]
        self.write(
            {
                "status": "confirmed",
                "counterpart_account_id": counterpart.id,
                "move_id": move.id,
                "move_line_id": bank_line.id if bank_line else False,
                "confirmed_at": fields.Datetime.now(),
                "confirmed_by_id": self.env.user.id,
            },
        )
        return move.id

    @api.model
    def _prepare_normalized_vals(self, vals):
        description = vals.get("description") or ""
        vals["description_key"] = self._normalize_description(description)
        vals["import_hash"] = self._build_import_hash(vals)
        return vals

    @api.model
    def _normalize_description(self, value):
        return re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).strip()

    @api.model
    def _build_import_hash(self, vals):
        company_id = vals.get("company_id") or self.env.company.id
        parts = [
            str(company_id),
            str(vals.get("date") or ""),
            str(vals.get("amount") or ""),
            vals.get("external_id") or "",
            vals.get("source_file") or "",
            str(vals.get("source_row") or ""),
            self._normalize_description(vals.get("description") or ""),
        ]
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

    def _assert_date_writable(self):
        self.ensure_one()
        period = self.env["poseidon.kernel.period"].search(
            [
                ("company_id", "=", self.company_id.id),
                ("date_from", "<=", self.date),
                ("date_to", ">=", self.date),
            ],
            limit=1,
        )
        if period and period.state != "open":
            raise UserError(_("Fiscal period is closed or locked. Use a reversal in an open period."))
        hard_lock_date = self.company_id.hard_lock_date
        if hard_lock_date and self.date <= hard_lock_date:
            raise UserError(_("Fiscal date is locked by company hard lock date."))

    def _assert_account_writable(self, account):
        self.ensure_one()
        self._assert_account_in_company(account)
        if not account.active:
            raise UserError(_("Account %(account)s is inactive.", account=account.display_name))
        if account.poseidon_kernel_locked:
            raise UserError(_("Account %(account)s is locked by the Poseidon kernel.", account=account.display_name))

    def _assert_account_in_company(self, account):
        self.ensure_one()
        if not account:
            raise ValidationError(_("Account is required."))
        if self.company_id not in account.company_ids:
            raise ValidationError(_("Account %(account)s does not belong to %(company)s.") % {
                "account": account.display_name,
                "company": self.company_id.display_name,
            })

    def _find_general_journal(self):
        self.ensure_one()
        journal = self.env["account.journal"].search(
            [
                ("company_id", "=", self.company_id.id),
                ("type", "=", "general"),
            ],
            limit=1,
        )
        if not journal:
            raise UserError(_("No general journal is configured for this company."))
        return journal
