from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "poseidon_kernel")
class TestKernelPeriodClose(TransactionCase):
    """Regression for the period-close AccessError (Workstream 3.1).

    A user with account.group_account_manager but NOT base.group_system must be
    able to close a Poseidon period, even though closing writes the company's
    hard_lock_date (a res.company write that ordinarily needs system rights).
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env["res.company"].create({"name": "Poseidon Close Co"})
        cls.accountant = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Poseidon Accountant",
                    "login": "poseidon_accountant_close",
                    "company_id": cls.company.id,
                    "company_ids": [(6, 0, [cls.company.id])],
                    "group_ids": [
                        (4, cls.env.ref("account.group_account_manager").id),
                    ],
                }
            )
        )

    def _new_period(self):
        return (
            self.env["poseidon.kernel.period"]
            .with_user(self.accountant)
            .create(
                {
                    "name": "2026-01",
                    "company_id": self.company.id,
                    "date_from": "2026-01-01",
                    "date_to": "2026-01-31",
                }
            )
        )

    def test_accountant_is_not_a_system_admin(self):
        # Guards the premise of the regression: if the accountant happened to be
        # a system admin the test would pass for the wrong reason.
        self.assertFalse(self.accountant.has_group("base.group_system"))

    def test_account_manager_can_close_period(self):
        period = self._new_period()
        # Must not raise AccessError on the res.company hard_lock_date write.
        period.action_close_period()
        self.assertEqual(period.state, "closed")
        self.assertEqual(period.closed_by_id, self.accountant)
        self.assertEqual(
            self.company.hard_lock_date,
            fields.Date.to_date("2026-01-31"),
        )

    def test_close_still_blocked_for_plain_user(self):
        # The elevation is scoped to the company write only; a user without the
        # kernel-period manager grant still cannot create/close periods.
        plain = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Poseidon Viewer",
                    "login": "poseidon_viewer_close",
                    "company_id": self.company.id,
                    "company_ids": [(6, 0, [self.company.id])],
                    "group_ids": [(4, self.env.ref("base.group_user").id)],
                }
            )
        )
        period = self._new_period()
        with self.assertRaises(AccessError):
            period.with_user(plain).action_close_period()
