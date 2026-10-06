from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import new_test_user


@tagged("post_install", "-at_install", "poseidon_kernel")
class TestAccountOperations(TransactionCase):
    def setUp(self):
        super().setUp()
        self.account = self.env["account.account"].create({
            "name": "Operations test", "code": "998781", "account_type": "expense",
            "company_ids": [(6, 0, [self.env.company.id])], "poseidon_kernel_locked": True,
        })

    def test_settings_do_not_mutate_locked_identity_and_keep_audit(self):
        result = self.account.poseidon_operations_update(self.env.company.id, self.account.id,
            "settings", {"responsible": "Controller", "notes": "Review monthly", "review_analytic": True})
        snapshot = self.account.poseidon_operations_snapshot(self.env.company.id, self.account.id)
        self.assertTrue(result["applied"])
        self.assertEqual(snapshot["account"]["settings"]["responsible"], "Controller")
        self.assertTrue(self.account.poseidon_kernel_locked)
        event = self.env["poseidon.account.operational.event"].browse(result["audit_ref"])
        with self.assertRaises(AccessError):
            event.write({"action": "tampered"})
        with self.assertRaises(AccessError):
            event.unlink()

    def test_rejects_foreign_account_and_link_target(self):
        other = self.env["res.company"].create({"name": "Operations other company"})
        foreign = self.env["account.account"].create({"name": "Foreign", "code": "998782",
            "account_type": "expense", "company_ids": [(6, 0, [other.id])]})
        with self.assertRaises(AccessError):
            self.account.poseidon_operations_snapshot(self.env.company.id, foreign.id)
        # A missing target cannot be accepted merely because its id is supplied.
        with self.assertRaises(AccessError), self.cr.savepoint():
            self.account.poseidon_operations_update(self.env.company.id, self.account.id,
                "add_link", {"relation": "revenue", "target_model": "account.analytic.account", "target_id": 2147483647})

    def test_process_link_add_remove_preserves_auditable_evidence(self):
        self.account.poseidon_operations_update(self.env.company.id, self.account.id,
            "add_link", {"relation": "process", "target_model": "process", "process_key": "real_estate"})
        snapshot = self.account.poseidon_operations_snapshot(self.env.company.id, self.account.id)
        link = snapshot["account"]["links"][0]
        self.account.poseidon_operations_update(self.env.company.id, self.account.id,
            "remove_link", {"link_id": link["id"]})
        snapshot = self.account.poseidon_operations_snapshot(self.env.company.id, self.account.id)
        self.assertEqual(snapshot["account"]["links"], [])
        self.assertEqual(len(snapshot["account"]["history"]), 2)

    def test_unknown_settings_and_invalid_period_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.account.poseidon_operations_update(self.env.company.id, self.account.id, "settings", {"code": "new"})
        with self.assertRaises(ValidationError):
            self.account.poseidon_operations_snapshot(self.env.company.id, self.account.id, "2026-10-31", "2026-10-01")

    def test_external_reference_requires_organization_and_has_no_sync_claim(self):
        self.account.poseidon_operations_update(self.env.company.id, self.account.id,
            "add_link", {"relation": "mapping", "target_model": "external", "platform": "xero",
                         "external_organization": "tenant-test", "external_account_id": "external-42", "external_account_name": "Rent income"})
        snapshot = self.account.poseidon_operations_snapshot(self.env.company.id, self.account.id)
        reference = next(m for m in snapshot["mappings"] if m["platform"] == "xero")
        self.assertEqual(reference["state"], "reference")
        self.assertEqual(reference["account_id"], self.account.id)
        self.assertEqual(reference["organization"], "tenant-test")
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self.account.poseidon_operations_update(self.env.company.id, self.account.id,
                "add_link", {"relation": "mapping", "target_model": "external", "platform": "xero",
                             "external_organization": "tenant-test", "external_account_id": "external-42", "external_account_name": "Duplicate"})
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self.account.poseidon_operations_update(self.env.company.id, self.account.id,
                "add_link", {"relation": "mapping", "target_model": "external", "platform": "xero",
                             "external_account_id": "external-42", "external_account_name": "Missing tenant"})

    def test_descriptions_and_illustrative_examples_are_distinct_from_ledger(self):
        self.account.poseidon_operations_update(self.env.company.id, self.account.id,
            "settings", {"description": "Used for reviewed rental costs", "examples": "Illustrative: debit expense, credit payable"})
        snapshot = self.account.poseidon_operations_snapshot(self.env.company.id, self.account.id)
        self.assertEqual(snapshot["account"]["settings"]["description"], "Used for reviewed rental costs")
        self.assertEqual(snapshot["account"]["posting_examples"], [])
        self.assertEqual(snapshot["account"]["posted"]["balance"], 0)

    def test_posted_closing_balance_excludes_drafts_and_preserves_counterparts(self):
        counterpart = self.env["account.account"].create({"name": "Counterpart", "code": "998783",
            "account_type": "equity", "company_ids": [(6, 0, [self.env.company.id])]})
        journal = self.env["account.journal"].create({"name": "Operations test journal", "code": "OPST",
            "type": "general", "company_id": self.env.company.id})
        def entry(date, amount, posted):
            move = self.env["account.move"].create({"journal_id": journal.id, "date": date,
                "line_ids": [(0, 0, {"name": "Account evidence", "account_id": self.account.id, "debit": amount}),
                             (0, 0, {"name": "Counterpart evidence", "account_id": counterpart.id, "credit": amount})]})
            if posted:
                move.action_post()
            return move
        entry("2026-09-01", 100, True)
        posted = entry("2026-09-10", 50, True)
        entry("2026-09-11", 999, False)
        snapshot = self.account.poseidon_operations_snapshot(self.env.company.id, self.account.id, "2026-09-05", "2026-09-30")
        self.assertEqual(snapshot["balances"][str(self.account.id)]["balance"], 150)
        self.assertEqual(snapshot["account"]["opening"], 100)
        self.assertEqual(snapshot["account"]["posted"]["balance"], 50)
        self.assertEqual(snapshot["account"]["draft"]["balance"], 999)
        self.assertEqual(snapshot["account"]["posting_examples"][0]["reference"], "account.move:%s" % posted.id)
        self.assertEqual(len(snapshot["account"]["posting_examples"][0]["lines"]), 2)
        self.assertEqual(snapshot["account"]["trend"][0]["net"], 50)

    def test_readonly_operator_can_read_but_cannot_change_metadata(self):
        reader = new_test_user(self.env, "operations_reader", groups="base.group_user,account.group_account_readonly")
        reader.write({"company_id": self.env.company.id, "company_ids": [(6, 0, [self.env.company.id])]})
        scoped = self.account.with_user(reader)
        snapshot = scoped.poseidon_operations_snapshot(self.env.company.id, self.account.id)
        self.assertFalse(snapshot["account"]["can_edit"])
        with self.assertRaises(AccessError):
            scoped.poseidon_operations_update(self.env.company.id, self.account.id, "settings", {"notes": "Unapproved"})
        if "poseidon.mcp.tools" in self.env:
            context = self.env["poseidon.mcp.tools"].with_user(reader)._execute_account_context(None, {"company_id": self.env.company.id, "account_id": self.account.id})
            self.assertEqual(context["reference"], "account.account:%s" % self.account.id)
            self.assertNotIn("history", context["account"])
