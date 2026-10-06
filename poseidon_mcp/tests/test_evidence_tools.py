from odoo.exceptions import AccessError, ValidationError
from odoo.tests import TransactionCase, new_test_user, tagged

from ..services.evidence_rank import rank


@tagged("post_install", "-at_install")
class TestEvidenceTools(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.foreign = cls.env["res.company"].create({"name": "Other evidence company"})
        cls.reader = new_test_user(cls.env, "evidence_reader", groups="base.group_user,account.group_account_user")
        cls.reader.write({"company_id": cls.company.id, "company_ids": [(6, 0, [cls.company.id])]})
        cls.Tools = cls.env["poseidon.mcp.tools"].with_user(cls.reader)
        cls.memory = cls.env["kodoo.ai.memory"].create({"name": "Reconciliação de caixa", "company_id": cls.company.id, "content_text": "Reconciliar caixa com extratos. Reviewed synthetic procedure."})
        cls.env["kodoo.ai.memory"].create({"name": "Foreign cash secret", "company_id": cls.foreign.id, "content_text": "Foreign cash secret evidence."})

    def _search(self, **changes):
        return self.Tools._execute_search_evidence(None, {"company_ids": [self.company.id], "query": "reconciliacao caixa", "sources": ["memory"], **changes})

    def test_accent_insensitive_search_has_canonical_provenance(self):
        result = self._search()
        self.assertEqual(result["results"][0]["reference"], "kodoo.ai.memory:%s" % self.memory.id)
        self.assertEqual(result["results"][0]["company_id"], self.company.id)
        self.assertTrue(result["results"][0]["updated_at"])
        self.assertNotIn("Foreign cash", str(result))

    def test_forbidden_scope_and_invalid_inputs_fail_closed(self):
        with self.assertRaises(AccessError):
            self._search(company_ids=[self.foreign.id])
        for values in ({"query": "x"}, {"limit": 21}, {"sources": ["ir.attachment"]}, {"company_ids": [True]}, {"company_ids": [self.company.id, self.company.id]}):
            with self.assertRaises(ValidationError):
                self._search(**values)

    def test_linked_memory_is_hidden_when_origin_is_outside_selection(self):
        account = self.env["account.account"].create({"name": "Foreign cash", "code": "FOREIGN100", "account_type": "asset_cash", "company_ids": [(6, 0, [self.foreign.id])]})
        self.reader.write({"company_ids": [(6, 0, [self.company.id, self.foreign.id])]})
        self.env["kodoo.ai.memory"].create({"name": "Do not leak", "company_id": False, "content_text": "Reconciliacao caixa SECRET", "source_type": "record", "source_model": "account.account", "source_res_id": account.id})
        self.assertNotIn("SECRET", str(self._search()))

    def test_recent_candidate_coverage_is_explicitly_truncated(self):
        self.env["kodoo.ai.memory"].create([{"name": "Candidate %s" % number, "company_id": self.company.id, "content_text": "Reconciliacao caixa."} for number in range(101)])
        result = self._search()
        self.assertTrue(result["coverage"][0]["truncated"])
        self.assertEqual(result["coverage"][0]["sampled"], 100)
        self.assertTrue(result["results_truncated"])

    def test_empty_search_is_not_an_unavailable_source(self):
        result = self._search(query="nonexistentword")
        self.assertEqual(result["results"], [])
        self.assertEqual(result["coverage"][0]["state"], "available")

    def test_rank_prefers_relevant_text_and_handles_empty_corpus(self):
        scores = rank("cash reconciliation", ["Cash reconciliation reviewed", "unrelated revenue"])
        self.assertGreater(scores[0], scores[1])
        self.assertEqual(rank("cash", []), [])

    def test_worklist_identifies_stale_controls_without_domain_writes(self):
        close = self.env["accounting.close"].create({"name": "Synthetic close", "date_start": "2026-09-01", "date_end": "2026-09-30", "company_id": self.company.id})
        before = self.env["accounting.close.audit"].search_count([])
        result = self.Tools._execute_close_worklist(None, {"company_ids": [self.company.id], "date_from": "2026-09-01", "date_to": "2026-09-30"})
        item = result["companies"][0]["closes"][0]
        self.assertFalse(result["companies"][0]["permissions"]["can_run_controls"])
        self.assertFalse(result["companies"][0]["permissions"]["can_prepare_period"])
        self.assertEqual(item["reference"], "accounting.close:%s" % close.id)
        self.assertEqual(item["control_evidence"], "missing_or_stale")
        self.assertEqual(self.env["accounting.close.audit"].search_count([]), before)
        self.assertEqual(close.state, "draft")

    def test_mcp_job_catalog_and_execution_match(self):
        Job = self.env["kodoo.mcp.job"].sudo()
        names = {tool["name"] for tool in Job.get_tool_catalog(self.reader)}
        self.assertIn("poseidon__search_evidence", names)
        self.assertIn("poseidon__close_worklist", names)
        job, _created = Job.create_or_get_job(operation_key="poseidon.search_evidence", payload={"company_ids": [self.company.id], "query": "caixa", "sources": ["memory"]}, user=self.reader)
        job.action_process_now()
        self.assertEqual(job.state, "done", job.error_message)
        self.assertIn("kodoo.ai.memory", job.result_json)
