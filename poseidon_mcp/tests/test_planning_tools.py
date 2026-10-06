from unittest.mock import patch

from odoo.exceptions import AccessError, ValidationError
from odoo.tests import TransactionCase, new_test_user, tagged


@tagged("post_install", "-at_install")
class TestPlanningTools(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Job = self.env["kodoo.mcp.job"].sudo()
        self.manager = new_test_user(self.env, "planning_manager", groups="base.group_user,account.group_account_manager,project.group_project_manager,meridian_saas.group_meridian_saas_accountant,qbo_bridge.group_qbo_bridge_manager")
        self.company = self.env.company
        self.manager.write({"company_id": self.company.id, "company_ids": [(6, 0, [self.company.id])]})
        self.Tools = self.env["poseidon.mcp.tools"].with_user(self.manager)

    def _job(self, operation, payload, user=None):
        job, created = self.Job.create_or_get_job(operation_key=operation, payload=payload, user=user or self.manager)
        if created:
            job.action_process_now()
        return job, created

    def _preview(self, actions):
        job, _ = self._job("poseidon.preview_setup", {"company_id": self.company.id, "actions": actions})
        self.assertEqual(job.state, "done", job.error_message)
        return job

    def _apply(self, preview, company_id=None):
        return self._job("poseidon.apply_setup", {"company_id": company_id or self.company.id, "preview_job_id": preview.id})

    def test_preview_creates_no_domain_records_and_apply_replays(self):
        Account = self.env["account.analytic.account"]
        count = Account.search_count([])
        preview = self._preview([{"kind": "cost_center", "name": "Operations", "code": "AI_OPS", "rationale": "Synthetic operator proposal."}])
        self.assertEqual(Account.search_count([]), count)
        applied, created = self._apply(preview)
        self.assertTrue(created)
        self.assertEqual(applied.state, "done", applied.error_message)
        self.assertEqual(Account.search_count([]), count + 1)
        replay, created = self._apply(preview)
        self.assertFalse(created)
        self.assertEqual(replay.id, applied.id)
        self.assertEqual(Account.search_count([]), count + 1)

    def test_stale_duplicate_is_rejected_without_partial_creation(self):
        actions = [{"kind": "cost_center", "name": "A", "code": "AI_A", "rationale": "Synthetic."},
                   {"kind": "cost_center", "name": "B", "code": "AI_B", "rationale": "Synthetic."}]
        preview = self._preview(actions)
        self.env["account.analytic.account"].poseidon_create_cost_center({"company_id": self.company.id, "name": "Changed", "code": "AI_B"})
        applied, _ = self._apply(preview)
        self.assertEqual(applied.state, "error")
        self.assertFalse(self.env["account.analytic.account"].search([("code", "=", "AI_A")]))

    def test_budget_and_project_are_created_as_reviewed_drafts(self):
        preview = self._preview([
            {"kind": "project", "name": "Synthetic project", "code": "AI_PROJECT", "rationale": "Explicit project proposal."},
            {"kind": "budget", "name": "Assumption budget", "date_from": "2026-01-01", "date_to": "2026-12-31", "basis": "assumption", "rationale": "Planning assumption, not actuals."},
        ])
        applied, _ = self._apply(preview)
        self.assertEqual(applied.state, "done", applied.error_message)
        self.assertTrue(self.env["project.project"].search([("name", "=", "Synthetic project")]))
        budget = self.env["crossovered.budget"].search([("name", "=", "Assumption budget")])
        self.assertEqual(budget.state, "draft")

    def test_preview_owner_and_accounting_role_are_revalidated(self):
        preview = self._preview([{"kind": "cost_center", "name": "A", "code": "AI_A", "rationale": "Synthetic."}])
        reader = new_test_user(self.env, "planning_reader")
        job, _ = self._job("poseidon.apply_setup", {"company_id": self.company.id, "preview_job_id": preview.id}, reader)
        self.assertEqual(job.state, "error")
        self.assertFalse(self.env["account.analytic.account"].search([("code", "=", "AI_A")]))

    def test_forbidden_company_and_unknown_kernel_are_rejected(self):
        with patch.object(type(self.env["res.company"]), "_create_internal_project_task", lambda self: None):
            foreign = self.env["res.company"].create({"name": "Foreign planning company"})
        with self.assertRaises(AccessError):
            self.Tools._planning_company(foreign.id)
        with self.assertRaises(ValidationError):
            self.Tools._planning_standard("INVENTED_CODE")

    def test_locked_period_blocks_apply(self):
        preview = self._preview([{"kind": "cost_center", "name": "A", "code": "AI_A", "rationale": "Synthetic."}])
        self.company.fiscalyear_lock_date = "2025-12-31"
        applied, _ = self._apply(preview)
        self.assertEqual(applied.state, "error")

    def test_history_does_not_infer_zero_actuals_from_empty_records(self):
        job, _ = self._job("poseidon.historical_evidence", {"company_ids": [self.company.id], "date_from": "2026-01-01", "date_to": "2026-12-31"})
        self.assertEqual(job.state, "done", job.error_message)
        data = job._load_json(job.result_json)
        self.assertEqual(data["companies"][0]["totals"], [])
        self.assertIn("not classified actuals", data["evidence_policy"])

    def _mapping_source(self):
        standard = self.env["qbo.standard.account"].create({"code": "AI600", "description": "Synthetic Services", "entry_type": "detail", "category": "Expense", "normal_balance": "debit", "odoo_account_type": "expense"})
        source = self.env["account.account"].create({"code": "AISOURCE", "name": "Source services", "account_type": "expense", "company_ids": [(6, 0, [self.company.id])], "qbo_id": "987654", "qbo_source_name": "Source services"})
        decision_id = self.env["poseidon.mapping.decision"].record_mapping_decision({"company_id": self.company.id, "source_account_id": source.id})
        return standard, source, self.env["poseidon.mapping.decision"].browse(decision_id)

    def test_mapping_confirmation_is_company_scoped_and_does_not_post_history(self):
        standard, source, decision = self._mapping_source()
        moves = self.env["account.move"].search_count([])
        preview = self._preview([{"kind": "mapping", "decision_id": decision.id, "kernel_code": standard.code, "rationale": "Reviewed synthetic type-compatible mapping."}])
        applied, _ = self._apply(preview)
        self.assertEqual(applied.state, "done", applied.error_message)
        self.assertEqual(decision.state, "confirmed")
        self.assertEqual(decision.bridge_rule_id.company_id, self.company)
        self.assertEqual(source.poseidon_parent_account_id, decision.destination_account_id)
        self.assertEqual(self.env["account.move"].search_count([]), moves)
        self.assertEqual(self.Tools._mapping_counts(self.company)["unresolved_sources"], 0)

    def test_unmapped_history_blocks_historical_budget_but_not_assumption_preview(self):
        self._mapping_source()
        values = {"kind": "budget", "name": "Not actuals", "date_from": "2026-01-01", "date_to": "2026-12-31", "basis": "historical", "rationale": "Synthetic."}
        with self.assertRaises(ValidationError):
            self.Tools._planning_actions({"company_id": self.company.id, "actions": [values]})
        self._preview([{**values, "basis": "assumption"}])

    def test_company_bridge_rule_precedes_shared_rule_without_leaking_to_another_company(self):
        Rule = self.env["qbo.account.bridge.rule"]
        shared = Rule.create({"match_name": "Synthetic exact name", "canonical_code": "SHARED", "canonical_name": "Shared", "canonical_account_type": "expense"})
        scoped = Rule.create({"company_id": self.company.id, "match_name": "Synthetic exact name", "canonical_code": "SCOPED", "canonical_name": "Scoped", "canonical_account_type": "expense"})
        self.assertEqual(Rule.with_company(self.company).match_qbo_record({"Name": "Synthetic exact name"}), scoped)
        with patch.object(type(self.env["res.company"]), "_create_internal_project_task", lambda self: None):
            foreign = self.env["res.company"].create({"name": "Other bridge company"})
        self.assertEqual(Rule.with_company(foreign).match_qbo_record({"Name": "Synthetic exact name"}), shared)

    def test_projection_requires_mapping_and_complete_observed_months(self):
        from datetime import date
        from ..models.poseidon_planning_tools import planning_run_rate
        monthly = [{"month": month, "revenue": 300, "expenses": 120, "net": 180} for month in ["2026-07", "2026-08", "2026-09"]]
        start, end = date(2026, 7, 1), date(2026, 9, 30)
        self.assertEqual(planning_run_rate(monthly, start, end, False)["state"], "blocked")
        self.assertEqual(planning_run_rate(monthly[:2], start, end, True)["state"], "insufficient_evidence")
        scenario = planning_run_rate(monthly, start, end, True)
        self.assertEqual(scenario["state"], "scenario")
        self.assertEqual(scenario["month"], "2026-10")
        self.assertEqual(scenario["net"], 180)
        self.assertFalse(scenario["applied"])

    def test_approved_setup_prepares_close_and_reconciliation_without_signoff(self):
        self.manager.write({"group_ids": [(4, self.env.ref("accounting_close.group_accounting_close_user").id)]})
        period = {"kind": "close_period", "name": "Synthetic agent close", "date_from": "2026-09-01", "date_to": "2026-09-30", "rationale": "Operator requested period preparation."}
        preview = self._preview([period])
        applied, _ = self._apply(preview)
        self.assertEqual(applied.state, "done", applied.error_message)
        result = applied._load_json(applied.result_json)["results"][0]
        close = self.env["accounting.close"].browse(result["id"])
        self.assertEqual(close.state, "draft")
        self.assertFalse(result["signed_off"])
        account = self.env["account.account"].create({"code": "CLOSETEST", "name": "Synthetic cash reconciliation", "account_type": "asset_cash", "company_ids": [(6, 0, [self.company.id])]})
        preview = self._preview([{"kind": "close_reconciliation", "close_id": close.id, "account_id": account.id, "subledger_balance": 150, "rationale": "Explicit synthetic statement balance."}])
        applied, _ = self._apply(preview)
        self.assertEqual(applied.state, "done", applied.error_message)
        self.assertEqual(close.reconciliation_ids.state, "not_started")
        self.assertFalse(close.reconciliation_ids.reviewed_by)
        replay, created = self._apply(preview)
        self.assertFalse(created)
        self.assertEqual(replay, applied)
        self.assertEqual(len(close.reconciliation_ids), 1)

    def test_close_preparation_requires_domain_role_and_revalidates_snapshot(self):
        from odoo.exceptions import AccessError
        period = {"kind": "close_period", "name": "Synthetic period", "date_from": "2026-09-01", "date_to": "2026-09-30", "rationale": "Synthetic."}
        with self.assertRaises(AccessError):
            self.Tools._planning_actions({"company_id": self.company.id, "actions": [period]})
        self.manager.write({"group_ids": [(4, self.env.ref("accounting_close.group_accounting_close_user").id)]})
        close = self.env["accounting.close"].create({"name": "Synthetic stale close", "date_start": "2026-09-01", "date_end": "2026-09-30"})
        account = self.env["account.account"].create({"code": "CLOSESTALE", "name": "Synthetic account", "account_type": "asset_cash", "company_ids": [(6, 0, [self.company.id])]})
        action = {"kind": "close_reconciliation", "close_id": close.id, "account_id": account.id, "subledger_balance": 0, "rationale": "An explicitly verified zero balance."}
        with self.assertRaises(ValidationError):
            self.Tools._planning_actions({"company_id": self.company.id, "actions": [{key: value for key, value in action.items() if key != "subledger_balance"}]})
        preview = self._preview([action])
        close.write({"name": "Changed after approval preview."})
        applied, _ = self._apply(preview)
        self.assertEqual(applied.state, "error")
        self.assertFalse(close.reconciliation_ids)

    def test_control_execution_requires_reviewer_and_replays_without_duplicate_evidence(self):
        close = self.env["accounting.close"].create({"name": "Synthetic control run", "date_start": "2026-09-01", "date_end": "2026-09-30"})
        action = {"kind": "close_controls", "close_id": close.id, "rationale": "Run the governed evidence suite on the reviewed snapshot."}
        with self.assertRaises(AccessError):
            self.Tools._planning_actions({"company_id": self.company.id, "actions": [action]})
        self.manager.write({"group_ids": [(4, self.env.ref("accounting_close.group_accounting_close_reviewer").id)]})
        preview = self._preview([action])
        self.assertFalse(close.sox_test_ids)
        applied, _ = self._apply(preview)
        self.assertEqual(applied.state, "done", applied.error_message)
        self.assertEqual(len(close.sox_test_ids), 5)
        self.assertEqual(close.state, "draft")
        replay, created = self._apply(preview)
        self.assertFalse(created)
        self.assertEqual(replay, applied)
        self.assertEqual(len(close.sox_test_ids), 5)
        stale_preview = self._preview([action])
        account = self.env["account.account"].create({"code": "CTRLSNAP", "name": "Synthetic changed snapshot", "account_type": "asset_cash", "company_ids": [(6, 0, [self.company.id])]})
        self.env["accounting.close.reconciliation"].create({"close_id": close.id, "account_id": account.id})
        blocked, _ = self._apply(stale_preview)
        self.assertEqual(blocked.state, "error")
        self.assertEqual(len(close.sox_test_ids), 5)
