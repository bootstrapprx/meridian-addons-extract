from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "poseidon_cost_center")
class TestObjectPlan(TransactionCase):
    def test_properties_is_the_root_operational_object_plan(self):
        properties = self.env.ref("industry_real_estate.analytic_plan_properties")
        object_plan = self.env["account.analytic.plan"]._poseidon_object_plan()
        self.assertEqual(object_plan, properties)
        self.assertEqual(object_plan.name, "Operational Object")
        self.assertFalse(object_plan.parent_id)
        self.assertEqual(object_plan.root_id, object_plan)

    def test_object_and_cost_center_are_distinct_roots(self):
        object_plan = self.env["account.analytic.plan"]._poseidon_object_plan()
        center_plan = self.env.ref("poseidon_cost_center.analytic_plan_cost_center")
        self.assertNotEqual(object_plan, center_plan)
        self.assertFalse(center_plan.parent_id)

    def test_legacy_third_plan_is_removed(self):
        self.assertFalse(self.env.ref(
            "poseidon_cost_center.analytic_plan_operational_object",
            raise_if_not_found=False,
        ))
        self.assertEqual(
            self.env["account.analytic.plan"].search_count([
                ("name", "=", "Operational Object"),
                ("parent_id", "=", False),
            ]),
            1,
        )

    def test_adoption_is_idempotent(self):
        before = self.env["account.analytic.plan"]._poseidon_object_plan()
        self.env["account.analytic.plan"]._poseidon_adopt_object_plan()
        self.env["account.analytic.plan"]._poseidon_adopt_object_plan()
        self.assertEqual(before, self.env["account.analytic.plan"]._poseidon_object_plan())

    def test_adoption_moves_legacy_accounts_before_removing_the_third_plan(self):
        legacy = self.env["account.analytic.plan"].create({"name": "Legacy Object"})
        self.env["ir.model.data"].create({
            "module": "poseidon_cost_center",
            "name": "analytic_plan_operational_object",
            "model": "account.analytic.plan",
            "res_id": legacy.id,
        })
        account = self.env["account.analytic.account"].create({
            "name": "Legacy Vehicle",
            "plan_id": legacy.id,
            "company_id": self.env.company.id,
        })

        properties = self.env["account.analytic.plan"]._poseidon_adopt_object_plan()

        self.assertEqual(account.plan_id, properties)
        self.assertFalse(legacy.exists())
