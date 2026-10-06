import base64
import io
from copy import deepcopy
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, new_test_user, tagged
from odoo.addons.qbo_bridge.services.qbo_sync_engine import QBOSyncEngine


@tagged("post_install", "-at_install")
class TestBooksIntake(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.manager = new_test_user(self.env, "books_manager", groups="base.group_user,account.group_account_manager,qbo_bridge.group_qbo_bridge_manager")
        self.reader = new_test_user(self.env, "books_reader", groups="base.group_user,account.group_account_user,qbo_bridge.group_qbo_bridge_manager")
        for user in (self.manager, self.reader):
            user.write({"company_ids": [(6, 0, self.company.ids)], "company_id": self.company.id})
        self.realm = self.env["qbo.realm"].create({"name": "Synthetic realm", "realm_id": "books-test", "client_id": "synthetic", "client_secret": "synthetic"})
        self.mapping = self.env["qbo.company.mapping"].create({"company_id": self.company.id, "realm_id": self.realm.id})
        self.accounts = self.env["account.account"]
        for i in (1, 2):
            account = self.env["account.account"].create({"name": "Synthetic books %s" % i, "code": "9910%s" % i,
                "company_ids": [(6, 0, self.company.ids)], "account_type": "expense", "qbo_id": "books-%s" % i})
            self.accounts |= account
            rule = self.env["qbo.account.bridge.rule"].create({"match_name": account.name,
                "canonical_code": account.code, "canonical_name": account.name, "canonical_account_type": "expense"})
            self.env["poseidon.mapping.decision"].create({"bridge_rule_id": rule.id,"company_id": self.company.id, "source_account_id": account.id,
                "destination_account_id": account.id, "qbo_id": account.qbo_id, "state": "confirmed"})
        self.env["account.journal"].create({"name": "Synthetic books", "code": "BKT", "type": "general", "company_id": self.company.id})
        self.Intake = self.env["poseidon.books.intake"].with_user(self.manager)
        self.payload = {"Id": "synthetic-je", "TxnDate": "2026-10-01", "Line": [
            {"Amount": 10, "DetailType": "JournalEntryLineDetail", "JournalEntryLineDetail": {"PostingType": side, "AccountRef": {"value": "books-%s" % i}}}
            for i, side in ((1, "Debit"), (2, "Credit"))]}

    def stage(self, payload=None):
        return self.Intake._stage_qbo(self.mapping.with_user(self.manager), [payload or self.payload])

    def test_ingress_retains_evidence_and_never_creates_moves(self):
        before = self.env["account.move"].search_count([])
        engine = QBOSyncEngine(self.Intake.env, self.mapping.with_user(self.manager))
        engine._upsert_journal_entries([self.payload])
        item = self.Intake.search([("source_key", "=", self.payload["Id"])])
        self.assertEqual(item.state, "ready")
        self.assertEqual(self.env["account.move"].search_count([]), before)
        self.assertEqual(self.stage(), item)
        self.assertEqual(self.env["poseidon.books.inbox"].search_count([("intake_id", "=", item.id)]), 1)

    def test_exact_approval_creates_one_draft_and_records_approver(self):
        item = self.stage()
        preview = item.action_prepare()
        result = item.action_approve_draft(preview["preview_hash"])
        self.assertEqual(item.move_id.state, "draft")
        self.assertEqual(item.approved_by, self.manager)
        self.assertEqual(result["move_id"], item.action_approve_draft(preview["preview_hash"])["move_id"])
        self.assertEqual(sum(item.move_id.line_ids.mapped("debit")), 10)

    def test_unmapped_account_and_invalid_rows_remain_in_intake(self):
        payload = deepcopy(self.payload)
        payload["Line"][0]["JournalEntryLineDetail"]["AccountRef"]["value"] = "missing"
        item = self.stage(payload)
        result = item.action_prepare()
        self.assertEqual(item.state, "blocked")
        self.assertIn("account_mapping", [b["kind"] for b in result["blockers"]])
        self.assertFalse(item.move_id)
        payload["Id"] = "invalid"
        payload["Line"][1]["Amount"] = "not money"
        self.assertEqual(self.stage(payload).action_prepare()["state"], "blocked")

    def test_report_rows_are_retained_instead_of_skipped(self):
        item = self.stage({"date": "2026-10-01", "debit": 100, "account": "Unknown"})
        self.assertEqual(item.action_prepare()["state"], "blocked")
        self.assertEqual(item.source_kind, "qbo_report")
        self.assertTrue(item.payload_json)

    def test_stale_setup_and_role_rejection(self):
        item = self.stage()
        preview = item.action_prepare()
        with self.assertRaises(AccessError):
            item.with_user(self.reader).action_approve_draft(preview["preview_hash"])
        self.accounts[0].write({"name": "Changed after review"})
        # Explicit timestamp guards also work when the transaction timestamp is equal.
        self.env["poseidon.mapping.decision"].search([("source_account_id", "=", self.accounts[0].id)]).write({"state": "pending"})
        with self.assertRaises(UserError):
            item.action_approve_draft(preview["preview_hash"])
        self.assertFalse(item.move_id)

    def test_personal_inbox_is_independent_and_cannot_be_reassigned(self):
        item = self.stage()
        Inbox = self.env["poseidon.books.inbox"]
        own = Inbox.with_user(self.manager).search([("intake_id", "=", item.id)])
        other = Inbox.with_user(self.reader).create({"intake_id": item.id})
        other.write({"is_read": True, "folder": "saved", "note": "Synthetic correction"})
        self.assertFalse(own.is_read)
        self.assertEqual(own.folder, "inbox")
        self.assertNotIn(other.id, Inbox.with_user(self.manager).search([]).ids)
        with self.assertRaises(AccessError):
            own.write({"user_id": self.reader.id})
        with self.assertRaises(AccessError):
            item.write({"state": "applied"})
        with self.assertRaises(AccessError):
            self.Intake.create({"name": "Forged source"})

    def test_forbidden_company_and_duplicate_existing_entry(self):
        forbidden = self.env["res.company"].create({"name": "Other workspace company"})
        with self.assertRaises(AccessError):
            self.Intake.list_for_review([forbidden.id])
        item = self.stage()
        item.action_prepare()
        item.action_approve_draft(item.preview_hash)
        revised = deepcopy(self.payload)
        revised["SyncToken"] = "1"
        revision = self.stage(revised)
        self.assertEqual(revision.action_prepare()["state"], "blocked")
        self.assertIn("existing_canonical_entry", [b["kind"] for b in revision.blockers_json])

    def test_durable_pull_uses_requester_and_creates_no_moves(self):
        Pull = self.env["poseidon.books.pull"].with_user(self.manager)
        task = Pull.request_history(self.company.id)
        self.assertEqual(task["id"], Pull.request_history(self.company.id)["id"])
        before = self.env["account.move"].search_count([])
        with patch("odoo.addons.qbo_bridge.services.qbo_api_client.QBOApiClient.get_journal_entries", return_value=[self.payload]):
            self.env["poseidon.books.pull"]._cron_receive_history()
        record = Pull.browse(task["id"])
        self.assertEqual(record.state, "done")
        self.assertEqual(record.received_count, 1)
        self.assertEqual(self.env["account.move"].search_count([]), before)

    def test_books_assistant_retains_selected_workspace_scope_and_provider(self):
        provider = self.env["kodoo.ai.provider"].create({"name": "Synthetic books provider", "provider": "odoo_chat", "model_name": "odoo_chat_local"})
        workspace = self.env["kodoo.ai.assistant"].search([("key", "=", "meridian_ai_agent")], limit=1)
        if not workspace:
            workspace = self.env["kodoo.ai.assistant"].create({"name": "Synthetic workspace", "key": "meridian_ai_agent"})
        workspace.write({"provider_id": provider.id})
        books = self.env.ref("poseidon_books_agent.books_assistant").with_user(self.manager)
        self.assertEqual(books._get_effective_provider(self.company.id), provider)
        self.assertTrue(books._uses_meridian_workspace_scope())
        payload = books._workspace_scope_payload({"read_company_ids": [self.company.id], "model_name": "auto/best-chat"}, self.manager)
        self.assertEqual(payload["read_company_ids"], [self.company.id])
        foreign = self.env["res.company"].create({"name": "Forbidden books scope"})
        with self.assertRaises(AccessError):
            books._workspace_scope_payload({"read_company_ids": [foreign.id]}, self.manager)

    def test_locked_date_and_unmapped_class_are_review_blockers(self):
        payload = deepcopy(self.payload)
        payload["Line"][0]["JournalEntryLineDetail"]["ClassRef"] = {"value": "missing-cost-center"}
        item = self.stage(payload)
        self.assertIn("cost_center", [b["kind"] for b in item.action_prepare()["blockers"]])
        self.company.write({"fiscalyear_lock_date": "2026-10-01"})
        self.assertIn("locked_period", [b["kind"] for b in item.action_prepare()["blockers"]])
        self.assertFalse(item.move_id)

    def test_currency_and_multiple_source_revisions_are_preserved(self):
        revised = deepcopy(self.payload)
        revised["SyncToken"] = "new"
        revised["CurrencyRef"] = {"value": "ZZZ"}
        items = self.Intake._stage_qbo(self.mapping.with_user(self.manager), [self.payload, revised])
        self.assertEqual(len(items), 2)
        blocked = items.filtered(lambda row: row.payload_json.get("CurrencyRef"))
        self.assertIn("foreign_currency", [b["kind"] for b in blocked.action_prepare()["blockers"]])
        self.assertFalse(blocked.move_id)

    def file_source(self, amount=10, entry_id="file-001"):
        text = "entry_id,date,account_code,debit,credit,description\n%s,2026-10-01,99101,%s,0,Debit\n%s,2026-10-01,99102,0,%s,Credit\n" % (entry_id, amount, entry_id, amount)
        result = self.Intake.stage_file(self.company.id, "journals.csv", base64.b64encode(text.encode()).decode(), "Other platform · test organization")
        self.assertEqual(result["received"], 1)
        return self.Intake.search([("source_kind", "=", "file_journal"), ("name", "ilike", entry_id)], order="id desc", limit=1)

    def test_file_dedup_preparation_and_exact_draft(self):
        before = self.env["account.move"].search_count([])
        item = self.file_source()
        self.assertEqual(item, self.file_source())
        self.assertEqual(item.state, "received")
        result = self.Intake.prepare_batch([self.company.id])
        self.assertEqual(result["prepared"], 1)
        self.assertEqual(result["readiness"][0]["intake"]["ready"], 1)
        self.assertEqual(self.env["account.move"].search_count([]), before)
        item.action_approve_draft(item.preview_hash)
        self.assertEqual(item.move_id.state, "draft")
        revised = self.file_source(20)
        self.assertNotEqual(item, revised)
        self.assertIn("existing_canonical_entry", [b["kind"] for b in revised.action_prepare()["blockers"]])

    def test_file_scope_and_schema_are_checked_before_staging(self):
        foreign = self.env["res.company"].create({"name": "Foreign file organization"})
        with self.assertRaises(AccessError):
            self.Intake.stage_file(foreign.id, "j.csv", "", "Other platform")
        with self.assertRaises(ValidationError):
            self.Intake.stage_file(self.company.id, "j.csv", base64.b64encode(b"arbitrary,export\n1,2").decode(), "Other platform")
        with self.assertRaises(ValidationError):
            self.Intake.prepare_batch([self.company.id], limit=26)

    def test_file_unmapped_accounts_block_without_moves(self):
        self.env["poseidon.mapping.decision"].search([("company_id", "=", self.company.id)]).write({"state": "pending"})
        item = self.file_source()
        self.assertIn("account_mapping", [b["kind"] for b in item.action_prepare()["blockers"]])
        self.assertFalse(item.move_id)

    def test_xlsx_normalizes_dates_and_retains_source(self):
        from datetime import datetime
        from openpyxl import Workbook
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["entry_id", "date", "account_code", "debit", "credit"])
        sheet.append(["xlsx-001", datetime(2026, 10, 1), 99101, 10, 0])
        sheet.append(["xlsx-001", datetime(2026, 10, 1), 99102, 0, 10])
        content = io.BytesIO()
        workbook.save(content)
        self.Intake.stage_file(self.company.id, "j.xlsx", base64.b64encode(content.getvalue()).decode(), "Spreadsheet organization")
        item = self.Intake.search([("source_kind", "=", "file_journal")])
        self.assertEqual(item.payload_json["rows"][0]["date"], "2026-10-01")
        self.assertEqual(item.action_prepare()["state"], "ready")

    def test_batch_cursor_covers_full_company_queue(self):
        for number in range(27):
            self.file_source(entry_id="batch-%s" % number)
        first = self.Intake.prepare_batch([self.company.id])
        self.assertEqual(first["prepared"], 25)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["readiness"][0]["intake"]["received"], 2)
        second = self.Intake.prepare_batch([self.company.id], first["next_cursor"])
        self.assertEqual(second["prepared"], 2)
        self.assertFalse(second["has_more"])
        self.assertEqual(second["readiness"][0]["intake"]["ready"], 27)
        self.assertFalse(self.Intake.search([("move_id", "!=", False)]))
