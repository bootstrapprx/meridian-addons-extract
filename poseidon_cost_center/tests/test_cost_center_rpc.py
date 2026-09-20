from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase, new_test_user, tagged


@tagged("post_install", "-at_install", "poseidon_cost_center")
class TestCostCenterRpc(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.plan = cls.env.ref("poseidon_cost_center.analytic_plan_cost_center")

    def _create(self, code, **values):
        return self.env["account.analytic.account"].poseidon_create_cost_center({
            "code": code,
            "name": values.pop("name", code),
            "company_id": self.company.id,
            **values,
        })

    def test_create_matches_the_bff_contract(self):
        item = self._create(
            "CC-RPC",
            name="Logistics",
            poseidon_cost_center_type="department",
            poseidon_cost_center_budget=2500.0,
            poseidon_cost_center_external_ref="QBO-CLASS-1",
        )
        self.assertEqual(set(item), {
            "id", "code", "name", "type", "owner", "company", "company_id",
            "currency", "budget", "debit", "credit", "balance", "active",
            "locked", "lock_reason", "external_ref", "plan",
        })
        self.assertEqual(item["type"], "department")
        self.assertEqual(item["budget"], 2500.0)
        self.assertEqual(item["external_ref"], "QBO-CLASS-1")
        self.assertEqual(
            self.env["account.analytic.account"].browse(item["id"]).root_plan_id,
            self.plan,
        )

    def test_snapshot_shape_and_summary(self):
        self._create("CC-SNAPSHOT", poseidon_cost_center_budget=1000.0)
        snapshot = self.env["account.analytic.account"].poseidon_cost_center_snapshot(
            self.company.id
        )
        self.assertEqual(
            set(snapshot["summary"]),
            {"count", "active", "locked", "budget", "balance", "variance"},
        )
        self.assertEqual(snapshot["summary"]["count"], len(snapshot["items"]))
        self.assertEqual(
            snapshot["summary"]["variance"],
            snapshot["summary"]["budget"] - abs(snapshot["summary"]["balance"]),
        )

    def test_false_company_and_other_plans_are_safe(self):
        other_plan = self.env["account.analytic.plan"].create({"name": "RPC Other"})
        self.env["account.analytic.account"].create({
            "name": "Not a cost center",
            "plan_id": other_plan.id,
            "company_id": self.company.id,
        })
        snapshot = self.env["account.analytic.account"].poseidon_cost_center_snapshot(False)
        self.assertNotIn("Not a cost center", [item["name"] for item in snapshot["items"]])

    def test_duplicate_code_in_a_company_is_refused(self):
        self._create("CC-DUP")
        with self.assertRaises(ValidationError):
            self._create("CC-DUP", name="Duplicate")

    def test_rpc_rejects_a_company_outside_the_user_scope(self):
        other = self.env["res.company"].create({"name": "Outside Scope"})
        user = new_test_user(
            self.env,
            login="cost-center-limited",
            groups="analytic.group_analytic_accounting",
            company_id=self.company.id,
            company_ids=[self.company.id],
        )
        model = self.env["account.analytic.account"].with_user(user)
        with self.assertRaises(AccessError):
            model.poseidon_cost_center_snapshot(other.id)
        with self.assertRaises(AccessError):
            model.poseidon_create_cost_center({
                "code": "CC-FORBIDDEN",
                "name": "Forbidden",
                "company_id": other.id,
            })

    def test_create_dimension_uses_the_selected_meridian_plan(self):
        object_plan = self.env.ref("industry_real_estate.analytic_plan_properties")
        item = self.env["account.analytic.account"].poseidon_create_analytic_dimension({
            "code": "ACT-RPC",
            "name": "Field activity",
            "plan_id": object_plan.id,
            "company_id": self.company.id,
        })
        account = self.env["account.analytic.account"].browse(item["id"])
        self.assertEqual(account.root_plan_id, object_plan)
        self.assertEqual(item["plan_id"], object_plan.id)
