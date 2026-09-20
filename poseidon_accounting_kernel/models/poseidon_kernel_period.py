from datetime import date

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class PoseidonKernelPeriod(models.Model):
    _name = "poseidon.kernel.period"
    _description = "Poseidon Fiscal Period Guardrail"
    _order = "date_from desc, id desc"

    # ref: old/aequitas-source-export/aequitas/backend/app/services/fiscal_period_service.py
    # ref: old/aequitas-source-export/aequitas/backend/app/services/validators/fiscal_period_guard.py
    name = fields.Char(required=True)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
        ondelete="cascade",
    )
    date_from = fields.Date(required=True, index=True)
    date_to = fields.Date(required=True, index=True)
    state = fields.Selection(
        [("open", "Open"), ("closed", "Closed"), ("locked", "Locked")],
        required=True,
        default="open",
        index=True,
    )
    closed_at = fields.Datetime(readonly=True, copy=False)
    closed_by_id = fields.Many2one("res.users", readonly=True, copy=False, ondelete="set null")
    locked_at = fields.Datetime(readonly=True, copy=False)
    locked_by_id = fields.Many2one("res.users", readonly=True, copy=False, ondelete="set null")
    lock_date_applied = fields.Date(readonly=True, copy=False)
    note = fields.Text()

    @api.constrains("date_from", "date_to")
    def _check_dates(self):
        for period in self:
            if period.date_from and period.date_to and period.date_from > period.date_to:
                raise ValidationError(_("Period start must be on or before period end."))

    @api.constrains("company_id", "date_from", "date_to")
    def _check_overlap(self):
        for period in self:
            if not period.company_id or not period.date_from or not period.date_to:
                continue
            overlap = self.search_count(
                [
                    ("id", "!=", period.id),
                    ("company_id", "=", period.company_id.id),
                    ("date_from", "<=", period.date_to),
                    ("date_to", ">=", period.date_from),
                ],
                limit=1,
            )
            if overlap:
                raise ValidationError(_("Poseidon fiscal periods cannot overlap for the same company."))

    def write(self, vals):
        if not self.env.context.get("poseidon_period_transition"):
            immutable = self.filtered(lambda period: period.state in {"closed", "locked"})
            if immutable and set(vals) != {"note"}:
                raise UserError(
                    _(
                        "Closed Poseidon periods are immutable. "
                        "Use a reversal in an open period instead of editing the closed period.",
                    ),
                )
            if "state" in vals and vals["state"] in {"closed", "locked"}:
                raise UserError(_("Use the close/lock actions to change a Poseidon period state."))
        return super().write(vals)

    def unlink(self):
        immutable = self.filtered(lambda period: period.state in {"closed", "locked"})
        if immutable:
            raise UserError(_("Closed or locked Poseidon periods cannot be removed."))
        return super().unlink()

    def action_close_period(self):
        for period in self:
            if period.state != "open":
                raise UserError(_("Only open Poseidon periods can be closed."))
            period._check_no_draft_moves()

        for period in self:
            period._apply_company_hard_lock_date()
            period.with_context(poseidon_period_transition=True).write(
                {
                    "state": "closed",
                    "closed_at": fields.Datetime.now(),
                    "closed_by_id": self.env.user.id,
                    "lock_date_applied": period.date_to,
                },
            )
            period._lock_period_accounts()
        return True

    def action_lock_period(self):
        for period in self:
            if period.state != "closed":
                raise UserError(_("Only closed Poseidon periods can be locked."))
            period.with_context(poseidon_period_transition=True).write(
                {
                    "state": "locked",
                    "locked_at": fields.Datetime.now(),
                    "locked_by_id": self.env.user.id,
                },
            )
        return True

    def _check_no_draft_moves(self):
        self.ensure_one()
        draft = self.env["account.move"].search(
            [
                ("company_id", "=", self.company_id.id),
                ("state", "=", "draft"),
                ("date", ">=", self.date_from),
                ("date", "<=", self.date_to),
            ],
            limit=1,
        )
        if draft:
            raise UserError(
                _(
                    "Cannot close Poseidon period %(period)s while draft entries exist. "
                    "Post or delete draft entry %(entry)s first.",
                    period=self.display_name,
                    entry=draft.display_name,
                ),
            )

    def _apply_company_hard_lock_date(self):
        self.ensure_one()
        lock_date = fields.Date.to_date(self.date_to)
        current = self.company_id.hard_lock_date or date.min
        if lock_date > current:
            # Authorized accounting users (account.group_account_manager) may
            # close a Poseidon period but cannot write res.company, which needs
            # base.group_system. Elevate ONLY this single lock-date write — the
            # narrowest elevation that lets a closing succeed. Auditability is
            # preserved: hard_lock_date is tracked on the company chatter and the
            # period stamps closed_by_id/closed_at; the field's own
            # never-go-backwards constraint still runs under sudo.
            self.company_id.sudo().write({"hard_lock_date": lock_date})

    def _lock_period_accounts(self):
        self.ensure_one()
        account_ids = self.env["account.move.line"].sudo().search(
            [
                ("company_id", "=", self.company_id.id),
                ("date", ">=", self.date_from),
                ("date", "<=", self.date_to),
            ],
        ).mapped("account_id")
        account_ids._poseidon_lock("period_close")
