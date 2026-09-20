# -*- coding: utf-8 -*-
# Part of Kodoo. See LICENSE file for full copyright and licensing details.

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase, new_test_user, tagged


@tagged("post_install", "-at_install", "poseidon_budget")
class TestBudgetRpc(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        # Odoo 19 makes account.analytic.account.plan_id NOT NULL, so the test
        # owns its plan instead of relying on one an unrelated module seeds.
        cls.analytic_plan = cls.env["account.analytic.plan"].create({
            "name": "RPC Budget Plan",
        })
        cls.analytic = cls.env["account.analytic.account"].create({
            "name": "RPC Cost Center",
            "company_id": cls.company.id,
            "plan_id": cls.analytic_plan.id,
        })
        cls.account = cls.env["account.account"].search([], limit=1)
        if not cls.account:
            cls.account = cls.env["account.account"].create({
                "name": "RPC Budget Account",
                "code": "RPC-BUDGET",
                "account_type": "asset_current",
                "company_ids": [(6, 0, cls.company.ids)],
            })
        cls.post = cls.env["account.budget.post"].create({
            "name": "RPC Position",
            "company_id": cls.company.id,
            "account_ids": [(6, 0, cls.account.ids)],
        })

    def _create(self, name="RPC Budget"):
        return self.env["crossovered.budget"].poseidon_create_budget({
            "name": name,
            "date_from": "2026-01-01",
            "date_to": "2026-12-31",
            "company_id": self.company.id,
        })

    def test_create_allocate_close_lifecycle(self):
        item = self._create()
        self.assertEqual(set(item), {
            "id", "name", "responsible_id", "responsible", "date_from",
            "date_to", "state", "company_id", "company", "lines", "projects",
            "total_planned", "total_practical", "total_theoritical",
        })
        self.assertEqual(item["state"], "draft")

        allocated = self.env["crossovered.budget"].poseidon_allocate_budget(
            item["id"],
            {
                "analytic_account_id": self.analytic.id,
                "general_budget_id": self.post.id,
                "planned_amount": -5000.0,
            },
        )
        self.assertEqual(len(allocated["lines"]), 1)
        self.assertEqual(allocated["lines"][0]["analytic_account_id"], self.analytic.id)
        self.assertEqual(allocated["total_planned"], -5000.0)
        self.assertEqual(allocated["projects"][0]["project"], "Unassigned")

        closed = self.env["crossovered.budget"].poseidon_budget_action(
            item["id"], "done"
        )
        self.assertEqual(closed["state"], "done")
        with self.assertRaises(ValidationError):
            self.env["crossovered.budget"].poseidon_allocate_budget(
                item["id"],
                {
                    "analytic_account_id": self.analytic.id,
                    "general_budget_id": self.post.id,
                    "planned_amount": -100.0,
                },
            )

    def test_allocate_against_project_account_is_grouped_by_project(self):
        # The project's analytic account is created explicitly: Odoo 19 requires
        # a plan on it, and a bare database has no default project plan.
        project_account = self.env["account.analytic.account"].create({
            "name": "RPC Project Account",
            "company_id": self.company.id,
            "plan_id": self.analytic_plan.id,
        })
        project = self.env["project.project"].create({
            "name": "RPC Project",
            "company_id": self.company.id,
            "account_id": project_account.id,
        })
        item = self._create(name="Project Budget")
        allocated = self.env["crossovered.budget"].poseidon_allocate_budget(
            item["id"],
            {
                "analytic_account_id": project.account_id.id,
                "general_budget_id": self.post.id,
                "planned_amount": -5000.0,
            },
        )
        self.assertEqual(allocated["lines"][0]["project_id"], project.id)
        self.assertEqual(allocated["lines"][0]["project"], project.name)
        self.assertEqual(len(allocated["projects"]), 1)
        self.assertEqual(allocated["projects"][0]["project_id"], project.id)
        self.assertEqual(allocated["projects"][0]["planned_amount"], -5000.0)

    def test_snapshot_shape_and_options(self):
        self._create()
        snapshot = self.env["crossovered.budget"].poseidon_budget_snapshot(
            self.company.id
        )
        self.assertEqual(set(snapshot["summary"]), {"count", "states", "total_planned"})
        self.assertEqual(snapshot["summary"]["count"], len(snapshot["items"]))
        self.assertTrue(snapshot["options"]["budget_posts"])
        self.assertTrue(
            any(
                account["id"] == self.analytic.id
                for account in snapshot["options"]["analytic_accounts"]
            )
        )

    def test_cross_company_allocation_is_refused(self):
        item = self._create()
        other = self.env["res.company"].create({"name": "Budget Outside"})
        outside_analytic = self.env["account.analytic.account"].create({
            "name": "Outside Center",
            "company_id": other.id,
            "plan_id": self.analytic_plan.id,
        })
        with self.assertRaises(ValidationError):
            self.env["crossovered.budget"].poseidon_allocate_budget(
                item["id"],
                {
                    "analytic_account_id": outside_analytic.id,
                    "general_budget_id": self.post.id,
                    "planned_amount": -100.0,
                },
            )

    def test_rpc_rejects_a_company_outside_the_user_scope(self):
        other = self.env["res.company"].create({"name": "Budget Scope"})
        user = new_test_user(
            self.env,
            login="budget-limited",
            groups="account.group_account_manager",
            company_id=self.company.id,
            company_ids=[self.company.id],
        )
        model = self.env["crossovered.budget"].with_user(user)
        with self.assertRaises(AccessError):
            model.poseidon_budget_snapshot(other.id)
        with self.assertRaises(AccessError):
            model.poseidon_create_budget({
                "name": "Forbidden",
                "date_from": "2026-01-01",
                "date_to": "2026-12-31",
                "company_id": other.id,
            })
