from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, new_test_user, tagged


@tagged("post_install", "-at_install")
class TestNormalizationTools(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.user = new_test_user(self.env, "normalization_reviewer", groups="base.group_user,account.group_account_manager,meridian_saas.group_meridian_saas_accountant,qbo_bridge.group_qbo_bridge_manager")
        self.user.write({"company_id": self.company.id, "company_ids": [(6, 0, self.company.ids)]})
        self.tools = self.env["poseidon.mcp.tools"].with_user(self.user)

    def test_review_distinguishes_sources_and_destinations_and_never_writes(self):
        account = self.env["account.account"]
        destination = account.create({"name": "Normalization rent", "code": "998811", "account_type": "expense", "company_ids": [(6, 0, self.company.ids)], "poseidon_kernel_layer": "L3"})
        source = account.create({"name": "Normalization source", "code": "998812", "account_type": "expense", "company_ids": [(6, 0, self.company.ids)], "poseidon_kernel_layer": "L3", "qbo_id": "normalization-source"})
        count = account.search_count([])
        lines = self.env["account.move.line"].search_count([])
        result = self.tools._execute_historical_normalization_review(None, {"company_id": self.company.id})
        self.assertIn(destination.id, [row["id"] for row in result["canonical_accounts"]])
        self.assertNotIn(source.id, [row["id"] for row in result["canonical_accounts"]])
        self.assertTrue(all(row["review_required"] for row in result["sources"]))
        self.assertFalse(result["applied"])
        self.assertFalse(result["qbo_writes"])
        self.assertEqual(account.search_count([]), count)
        self.assertEqual(self.env["account.move.line"].search_count([]), lines)

    def test_review_rejects_a_foreign_company(self):
        company = self.env["res.company"].create({"name": "Unrelated normalization company"})
        with self.assertRaises(AccessError):
            self.tools._execute_historical_normalization_review(None, {"company_id": company.id})

    def test_operation_runs_through_the_actor_bound_job_dispatcher(self):
        job, created = self.env["kodoo.mcp.job"].create_or_get_job(
            operation_key="poseidon.historical_normalization_review",
            payload={"company_id": self.company.id}, user=self.user,
        )
        self.assertTrue(created)
        job.action_process_now()
        self.assertEqual(job.state, "done", job.error_message)

    def test_inactive_l3_destinations_are_prepared_as_activation_proposals(self):
        master = self.env["qbo.standard.account"].create({
            "code": "998899", "description": "Normalization new rent", "entry_type": "detail",
            "category": "Expense", "normal_balance": "debit", "odoo_account_type": "expense",
            "kernel_layer": "L3", "active": True,
        })
        self.env["account.account"].create({
            "name": "Normalization new rent", "code": "998898", "account_type": "expense",
            "company_ids": [(6, 0, self.company.ids)], "qbo_id": "normalization-new-rent",
        })
        result = self.tools._execute_historical_normalization_review(None, {"company_id": self.company.id})
        candidate = next(row for source in result["sources"] for row in source["candidates"] if row["standard_id"] == master.id)
        self.assertTrue(candidate["activation_required"])
        self.assertIsNone(candidate["account_id"])
        self.assertFalse(result["applied"])

    def test_activation_cannot_transform_a_historical_source_account(self):
        master = self.env["qbo.standard.account"].create({
            "code": "998887", "description": "Canonical destination", "entry_type": "detail",
            "category": "Expense", "normal_balance": "debit", "odoo_account_type": "expense",
            "kernel_layer": "L3", "active": True,
        })
        source = self.env["account.account"].create({
            "name": "Preserved historical source", "code": master.code, "account_type": "expense",
            "company_ids": [(6, 0, self.company.ids)], "qbo_id": "preserved-history",
        })
        with self.assertRaises(UserError):
            self.env["account.chart.template"].poseidon_activate_standard_account_for_company(self.company.id, master.id)
        self.assertFalse(source.qbo_standard_account_id)
        self.assertFalse(source.poseidon_kernel_layer)
        self.assertEqual(source.name, "Preserved historical source")
        skipped = master._publish_kernel_standards_to_company(self.company, master)
        self.assertEqual(skipped["metadata_skipped"], 1)
        self.assertFalse(source.qbo_standard_account_id)
        source.write({"qbo_standard_account_id": master.id})
        with self.assertRaises(UserError):
            self.env["account.chart.template"].poseidon_activate_standard_account_for_company(self.company.id, master.id)
        self.assertFalse(master._safe_apply_kernel_metadata(source, master))
        self.assertFalse(source.poseidon_kernel_layer)
        self.assertEqual(source.qbo_id, "preserved-history")
        stats = master._publish_kernel_standards_to_company(self.company, master)
        self.assertEqual(stats["metadata_skipped"], 1)
        self.assertEqual(stats["metadata_updated"], 0)
        self.assertEqual(source.name, "Preserved historical source")

    def test_evidence_follows_target_company_instead_of_user_default(self):
        company = self.env["res.company"].create({"name": "Authorized second review company"})
        self.user.write({"company_ids": [(6, 0, (self.company | company).ids)]})
        account_model = self.env["account.account"].with_company(company)
        source = account_model.create({"name": "Second company source", "code": "998871", "account_type": "expense", "company_ids": [(6, 0, company.ids)], "qbo_id": "second-company-source"})
        counterpart = account_model.create({"name": "Second company equity", "code": "998872", "account_type": "equity", "company_ids": [(6, 0, company.ids)]})
        journal = self.env["account.journal"].with_company(company).create({"name": "Normalization scope evidence", "code": "NORM", "type": "general", "company_id": company.id})
        move = self.env["account.move"].with_company(company).create({"journal_id": journal.id, "company_id": company.id,
            "line_ids": [(0, 0, {"account_id": source.id, "name": "Evidence in the second company", "debit": 1}), (0, 0, {"account_id": counterpart.id, "credit": 1})]})
        result = self.tools.with_context(allowed_company_ids=self.company.ids)._execute_historical_normalization_review(None, {"company_id": company.id})
        self.assertEqual(self.user.company_id, self.company)
        sample = next(row for row in result["sources"] if row["source_id"] == source.id)["historical_samples"]
        self.assertEqual(len(sample), 1)
        self.assertEqual(sample[0]["document_reference"], "account.move:%s" % move.id)
