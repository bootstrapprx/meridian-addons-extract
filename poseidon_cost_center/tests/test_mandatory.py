from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "poseidon_cost_center")
class TestMandatoryDimension(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.env["account.analytic.account"].poseidon_create_cost_center({
            "code": "CC-MANDATORY",
            "name": "Mandatory Gate",
            "company_id": cls.company.id,
        })
        cls.expense = cls.env["account.account"].with_company(cls.company).create({
            "name": "Mandatory Expense",
            "code": "69882",
            "account_type": "expense",
            "company_ids": [(6, 0, [cls.company.id])],
        })
        cls.asset = cls.env["account.account"].with_company(cls.company).create({
            "name": "Non-mandatory Asset",
            "code": "19882",
            "account_type": "asset_current",
            "company_ids": [(6, 0, [cls.company.id])],
        })
        payable = cls.env["account.account"].with_company(cls.company).create({
            "name": "Mandatory Payable",
            "code": "29882",
            "account_type": "liability_payable",
            "reconcile": True,
            "company_ids": [(6, 0, [cls.company.id])],
        })
        cls.partner = cls.env["res.partner"].create({"name": "Mandatory Vendor"})
        cls.partner.with_company(cls.company).property_account_payable_id = payable

    def _bill(self, account):
        return self.env["account.move"].with_company(self.company).create({
            "move_type": "in_invoice",
            "partner_id": self.partner.id,
            "invoice_date": fields.Date.today(),
            "invoice_line_ids": [(0, 0, {
                "name": "Thing",
                "account_id": account.id,
                "quantity": 1,
                "price_unit": 10.0,
            })],
        })

    def test_untagged_expense_bill_cannot_post(self):
        with self.assertRaises(ValidationError):
            self._bill(self.expense).action_post()

    def test_untagged_balance_sheet_bill_posts(self):
        move = self._bill(self.asset)
        move.action_post()
        self.assertEqual(move.state, "posted")

    def test_rules_are_global_mandatory_and_prefix_scoped(self):
        expected = {
            "applicability_cost_center_bill": ("bill", "5,6"),
            "applicability_cost_center_invoice": ("invoice", "4"),
            "applicability_cost_center_timesheet": ("timesheet", False),
        }
        for xmlid, (domain, prefix) in expected.items():
            rule = self.env.ref("poseidon_cost_center.%s" % xmlid)
            self.assertEqual(rule.applicability, "mandatory")
            self.assertEqual(rule.business_domain, domain)
            self.assertEqual(rule.account_prefix, prefix)
            self.assertFalse(rule.company_id)

    def test_vehicle_is_offered_on_expense_bill_lines(self):
        self.assertTrue(self._bill(self.expense).invoice_line_ids.need_vehicle)
