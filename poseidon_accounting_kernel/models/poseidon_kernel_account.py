from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


class AccountAccount(models.Model):
    _inherit = "account.account"

    # ref: old/aequitas-source-export/aequitas/backend/app/services/companychart_service.py
    # ref: old/aequitas-source-export/aequitas/backend/app/db/models/company_account.py
    POSEIDON_IMMUTABLE_FIELDS = {
        "name",
        "code",
        "account_type",
        "company_ids",
        "poseidon_kernel_version_id",
        "poseidon_kernel_layer",
        "poseidon_kernel_code",
        "poseidon_kernel_required",
        "poseidon_master_account_id",
        "poseidon_account_category",
        "poseidon_normal_balance",
        "poseidon_fs_mapping",
        "poseidon_parent_account_id",
    }

    poseidon_kernel_version_id = fields.Many2one(
        "poseidon.kernel.version",
        string="Poseidon kernel version",
        ondelete="restrict",
        copy=False,
        index=True,
    )
    poseidon_kernel_layer = fields.Selection(
        [("L0", "L0"), ("L1", "L1"), ("L2", "L2"), ("L3", "L3")],
        string="Poseidon kernel layer",
        copy=False,
        index=True,
    )
    poseidon_kernel_code = fields.Char(
        string="Poseidon canonical code",
        copy=False,
        index=True,
    )
    poseidon_kernel_required = fields.Boolean(
        string="Required by kernel",
        copy=False,
    )
    poseidon_master_account_id = fields.Char(
        string="Poseidon master account UUID",
        copy=False,
        index=True,
    )
    poseidon_account_category = fields.Selection(
        [
            ("ASSET", "Asset"),
            ("LIABILITY", "Liability"),
            ("EQUITY", "Equity"),
            ("REVENUE", "Revenue"),
            ("COST_OF_GOODS_SOLD", "Cost of Goods Sold"),
            ("EXPENSE", "Expense"),
        ],
        string="Poseidon account category",
        copy=False,
        index=True,
    )
    poseidon_normal_balance = fields.Selection(
        [("Debit", "Debit"), ("Credit", "Credit")],
        string="Poseidon normal balance",
        copy=False,
    )
    poseidon_fs_mapping = fields.Selection(
        [("Balance Sheet", "Balance Sheet"), ("Income Statement", "Income Statement")],
        string="Poseidon financial statement",
        copy=False,
    )
    poseidon_parent_account_id = fields.Many2one(
        "account.account",
        string="Poseidon parent account",
        ondelete="restrict",
        copy=False,
        index=True,
    )
    poseidon_kernel_locked = fields.Boolean(
        string="Poseidon locked",
        copy=False,
        index=True,
        help="Locked after the first journal item or a manual lock.",
    )
    poseidon_kernel_locked_at = fields.Datetime(
        string="Poseidon locked at",
        copy=False,
        readonly=True,
    )
    poseidon_kernel_locked_by = fields.Many2one(
        "res.users",
        string="Poseidon locked by",
        ondelete="set null",
        copy=False,
        readonly=True,
    )
    poseidon_kernel_lock_reason = fields.Selection(
        [
            ("first_transaction", "First transaction"),
            ("period_close", "Period close"),
            ("manual", "Manual"),
        ],
        string="Poseidon lock reason",
        copy=False,
        readonly=True,
    )
    poseidon_qbo_push_approved = fields.Boolean(
        string="Approved for QBO Push",
        default=False,
        copy=False,
        help="Approved to be pushed/synced to QuickBooks Online.",
    )

    def write(self, vals):
        if not self.env.context.get("poseidon_kernel_skip_lock_check"):
            self._poseidon_check_locked_write(vals)
        return super().write(vals)

    def unlink(self):
        blocked = self.filtered(lambda account: account.poseidon_kernel_locked or account._poseidon_has_move_lines())
        if blocked:
            names = ", ".join(blocked[:5].mapped("display_name"))
            raise UserError(
                _(
                    "Cannot delete Poseidon locked accounts or accounts with journal items. "
                    "Use deactivation or a forward correction instead. Accounts: %(accounts)s",
                    accounts=names,
                ),
            )
        return super().unlink()

    def action_poseidon_lock_account(self):
        self._poseidon_lock("manual", user=self.env.user)
        return True

    @api.model
    def poseidon_create_subaccount(self, parent_account_id, company_id, code, name):
        company = self.env["res.company"].browse(int(company_id or 0)).exists()
        if not company or company not in self.env.user.company_ids:
            raise AccessError(_("You do not have access to the requested company."))

        code = str(code or "").strip()
        name = str(name or "").strip()
        if not code or not name:
            raise ValidationError(_("Subaccount code and name are required."))
        if len(code) > 64 or len(name) > 256:
            raise ValidationError(_("Subaccount code or name is too long."))

        scoped = self.with_company(company).with_context(
            allowed_company_ids=[company.id], active_test=False
        )
        parent = scoped.search([
            ("id", "=", int(parent_account_id or 0)),
            ("company_ids", "in", company.id),
        ], limit=1)
        if not parent:
            raise ValidationError(_("The parent account is not available in this company."))
        if not parent.poseidon_kernel_layer:
            raise ValidationError(_("Subaccounts must be created under a kernel account."))
        if parent.account_type in ("asset_receivable", "liability_payable"):
            raise ValidationError(_("Receivable and payable control accounts cannot have subaccounts."))
        if scoped.search_count([
            ("code", "=", code),
            ("company_ids", "in", company.id),
        ], limit=1):
            raise ValidationError(_("Account code %(code)s already exists in this company.", code=code))

        account = scoped.create({
            "name": name,
            "code": code,
            "account_type": parent.account_type,
            "company_ids": [(6, 0, [company.id])],
            "poseidon_parent_account_id": parent.id,
        })
        return {
            "id": account.id,
            "code": account.code,
            "name": account.name,
            "account_type": account.account_type,
            "parent_id": parent.id,
            "parent_code": parent.code,
        }

    @api.model
    def _poseidon_lock_accounts_with_transactions(self):
        account_ids = self._get_used_account_ids()
        if not account_ids:
            return True
        accounts = self.with_context(active_test=False).search(
            [
                ("id", "in", account_ids),
                ("poseidon_kernel_locked", "=", False),
            ],
        )
        accounts._poseidon_lock("first_transaction")
        return True

    def _poseidon_lock(self, reason, user=None):
        accounts = self.filtered(lambda account: not account.poseidon_kernel_locked)
        if not accounts:
            return True
        vals = {
            "poseidon_kernel_locked": True,
            "poseidon_kernel_locked_at": fields.Datetime.now(),
            "poseidon_kernel_lock_reason": reason,
        }
        if user:
            vals["poseidon_kernel_locked_by"] = user.id
        accounts.with_context(poseidon_kernel_skip_lock_check=True).write(vals)
        return True

    def _poseidon_check_locked_write(self, vals):
        protected = self.POSEIDON_IMMUTABLE_FIELDS & vals.keys()
        if not protected:
            return
        locked = self.filtered(lambda account: account.poseidon_kernel_locked or account._poseidon_has_move_lines())
        if not locked:
            return
        field_names = self.fields_get(list(protected))
        labels = ", ".join(field_names[field]["string"] for field in protected)
        accounts = ", ".join(locked[:5].mapped("display_name"))
        raise UserError(
            _(
                "Cannot modify immutable Poseidon account fields after the first transaction "
                "or manual lock. Fields: %(fields)s. Accounts: %(accounts)s",
                fields=labels,
                accounts=accounts,
            ),
        )

    def _poseidon_has_move_lines(self):
        self.ensure_one()
        return bool(
            self.env["account.move.line"].sudo().search_count(
                [("account_id", "=", self.id)],
                limit=1,
            ),
        )


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        lines.mapped("account_id")._poseidon_lock("first_transaction")
        return lines
