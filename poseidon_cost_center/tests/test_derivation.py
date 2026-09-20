from odoo import fields
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "poseidon_cost_center")
class TestDerivation(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.center = cls.env["account.analytic.account"].browse(
            cls.env["account.analytic.account"].poseidon_create_cost_center({
                "code": "CC-DERIVE",
                "name": "Logistics",
                "company_id": cls.company.id,
            })["id"]
        )
        model = cls.env["fleet.vehicle.model"].search([], limit=1)
        if not model:
            make = cls.env["fleet.vehicle.model.brand"].create({"name": "Meridian"})
            model = cls.env["fleet.vehicle.model"].create({
                "name": "Van",
                "brand_id": make.id,
            })
        cls.vehicle = cls.env["fleet.vehicle"].create({
            "model_id": model.id,
            "license_plate": "VAN-01",
            "company_id": cls.company.id,
            "cost_center_id": cls.center.id,
        })
        cls.expense = cls.env["account.account"].with_company(cls.company).create({
            "name": "Derivation Expense",
            "code": "69881",
            "account_type": "expense",
            "company_ids": [(6, 0, [cls.company.id])],
        })
        payable = cls.env["account.account"].with_company(cls.company).create({
            "name": "Derivation Payable",
            "code": "29881",
            "account_type": "liability_payable",
            "reconcile": True,
            "company_ids": [(6, 0, [cls.company.id])],
        })
        cls.partner = cls.env["res.partner"].create({"name": "Vehicle Vendor"})
        cls.partner.with_company(cls.company).property_account_payable_id = payable

    def _bill_line(self, vehicle=False):
        return self.env["account.move"].with_company(self.company).create({
            "move_type": "in_invoice",
            "partner_id": self.partner.id,
            "invoice_date": fields.Date.today(),
            "invoice_line_ids": [(0, 0, {
                "name": "Vehicle cost",
                "account_id": self.expense.id,
                "quantity": 1,
                "price_unit": 100.0,
                "vehicle_id": vehicle.id if vehicle else False,
            })],
        }).invoice_line_ids

    def test_vehicle_object_account_is_created_once_on_the_adopted_plan(self):
        account = self.vehicle._poseidon_object_account()
        self.assertEqual(account, self.vehicle._poseidon_object_account())
        self.assertEqual(
            account.root_plan_id,
            self.env["account.analytic.plan"]._poseidon_object_plan(),
        )
        self.assertEqual(account.poseidon_object_center_id, self.center)

    def test_vehicle_derives_both_dimensions(self):
        line = self._bill_line(self.vehicle)
        tagged_ids = {
            int(account_id)
            for key in (line.analytic_distribution or {})
            for account_id in key.split(",")
        }
        self.assertIn(self.vehicle._poseidon_object_account().id, tagged_ids)
        self.assertIn(self.center.id, tagged_ids)

    def test_post_materializes_both_plan_columns_on_one_analytic_line(self):
        line = self._bill_line(self.vehicle)
        line.move_id.action_post()
        object_account = self.vehicle._poseidon_object_account()
        object_column = object_account.root_plan_id._column_name()
        center_column = self.center.root_plan_id._column_name()
        analytic_lines = self.env["account.analytic.line"].search([
            ("move_line_id", "=", line.id),
        ])
        self.assertTrue(analytic_lines.filtered(
            lambda item: item[object_column] == object_account
            and item[center_column] == self.center
        ))

    def test_setting_vehicle_later_retriggers_derivation(self):
        line = self._bill_line()
        self.assertFalse(line.analytic_distribution)
        line.vehicle_id = self.vehicle
        tagged_ids = {
            int(account_id)
            for key in (line.analytic_distribution or {})
            for account_id in key.split(",")
        }
        self.assertIn(self.vehicle._poseidon_object_account().id, tagged_ids)

    def test_unrelated_line_is_left_to_the_base_chain(self):
        self.assertFalse(self._bill_line().analytic_distribution)

    def test_qbo_class_materializes_a_posted_historical_cost(self):
        if "qbo.company.mapping" not in self.env:
            self.skipTest("qbo_bridge is not installed")
        self.center.poseidon_cost_center_external_ref = "QBO-CLASS-42"
        self.expense.qbo_id = "QBO-EXPENSE-42"
        self.partner.write({"qbo_id": "QBO-VENDOR-42"})
        journal = self.env["account.journal"].search([
            ("company_id", "=", self.company.id),
            ("type", "=", "purchase"),
        ], limit=1)
        if not journal:
            journal = self.env["account.journal"].create({
                "name": "Historical Bills",
                "code": "HBL",
                "type": "purchase",
                "company_id": self.company.id,
            })
        realm = self.env["qbo.realm"].create({
            "name": "Class Mapping Realm",
            "realm_id": "class-mapping-realm",
            "client_id": "test",
            "client_secret": "test",
        })
        mapping = self.env["qbo.company.mapping"].create({
            "company_id": self.company.id,
            "realm_id": realm.id,
        })
        from odoo.addons.qbo_bridge.services.qbo_sync_engine import QBOSyncEngine

        QBOSyncEngine(self.env, mapping)._upsert_invoices([{
            "Id": "QBO-BILL-CLASS-42",
            "SyncToken": "0",
            "DocNumber": "BILL-CLASS-42",
            "TxnDate": fields.Date.to_string(fields.Date.today()),
            "VendorRef": {"value": "QBO-VENDOR-42", "name": self.partner.name},
            "Line": [{
                "Amount": 125.0,
                "Description": "Historical logistics cost",
                "DetailType": "AccountBasedExpenseLineDetail",
                "AccountBasedExpenseLineDetail": {
                    "AccountRef": {"value": "QBO-EXPENSE-42", "name": self.expense.name},
                    "ClassRef": {"value": "QBO-CLASS-42", "name": "Logistics"},
                },
            }],
            "_qbo_type": "bill",
        }])
        move = self.env["account.move"].search([("qbo_id", "=", "QBO-BILL-CLASS-42")])
        self.assertEqual(
            move.invoice_line_ids.analytic_distribution,
            {str(self.center.id): 100.0},
        )
        move.action_post()
        center_column = self.center.root_plan_id._column_name()
        analytic_line = self.env["account.analytic.line"].search([
            ("move_line_id", "=", move.invoice_line_ids.id),
        ])
        self.assertEqual(analytic_line[center_column], self.center)
        self.assertEqual(analytic_line.amount, -125.0)
