"""Prepare-environment tests (ClickUp 86bb6vcpf, Phase 2).

Prepare is the only write in Slice 1. These tests pin the two invariants that
matter: it creates journals and nothing else, and it refuses to run without an
explicit human confirmation.
"""
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestQboPrepare(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env["res.company"].create({"name": "Prepare Corp"})
        self.realm = self.env["qbo.realm"].create({
            "name": "Prepare Realm",
            "realm_id": "444555666",
            "client_id": "cid",
            "client_secret": "secret",
            "state": "connected",
            "sync_mode": "pull_only",
            "refresh_token": "rt",
        })
        self.mapping = self.env["qbo.company.mapping"].create({
            "company_id": self.company.id,
            "realm_id": self.realm.id,
        })
        self.env["account.journal"].search(
            [("company_id", "=", self.company.id), ("type", "in", ("sale", "purchase", "general"))]
        ).unlink()
        # Seed AR/AP so journals are the ONLY remaining blocker — otherwise
        # readiness stays "blocked" after prepare and the state assertion below
        # would be testing the chart gap, not prepare.
        for code, name, account_type in (
            ("11000", "Accounts Receivable", "asset_receivable"),
            ("21000", "Accounts Payable", "liability_payable"),
        ):
            self.env["account.account"].create({
                "name": name,
                "code": code,
                "account_type": account_type,
                "company_ids": [(4, self.company.id)],
            })

    def _journal_types(self):
        return set(
            self.env["account.journal"]
            .search([("company_id", "=", self.company.id)])
            .mapped("type")
        )

    def test_preview_lists_planned_journals_without_creating_them(self):
        result = self.mapping.preview_sync_prerequisites()
        self.assertTrue(result["preview_only"])
        self.assertEqual(
            {row["type"] for row in result["planned"]},
            {"sale", "purchase", "general"},
        )
        self.assertFalse(self._journal_types() & {"sale", "purchase", "general"})

    def test_commit_without_confirmation_is_refused(self):
        with self.assertRaises(UserError):
            self.mapping.prepare_sync_prerequisites()
        self.assertFalse(self._journal_types() & {"sale", "purchase", "general"})

    def test_commit_creates_the_missing_journals(self):
        result = self.mapping.prepare_sync_prerequisites(human_confirmed=True)
        self.assertEqual(len(result["created"]), 3)
        self.assertTrue({"sale", "purchase", "general"} <= self._journal_types())
        self.assertEqual(self.mapping.onboarding_state, "setup_prepared")

    def test_commit_is_idempotent(self):
        self.mapping.prepare_sync_prerequisites(human_confirmed=True)
        second = self.mapping.prepare_sync_prerequisites(human_confirmed=True)
        self.assertEqual(second["created"], [])

    def test_commit_never_creates_an_account(self):
        before = self.env["account.account"].search_count(
            [("company_ids", "=", self.company.id)]
        )
        self.mapping.prepare_sync_prerequisites(human_confirmed=True)
        after = self.env["account.account"].search_count(
            [("company_ids", "=", self.company.id)]
        )
        self.assertEqual(before, after)

    def test_prepare_links_imported_bank_accounts_to_new_bank_journals(self):
        bank = self.env["account.account"].create({
            "name": "QBO Checking",
            "code": "10100",
            "account_type": "asset_cash",
            "company_ids": [(4, self.company.id)],
            "qbo_id": "QBO-BANK-1",
            "qbo_source_account_type": "Bank",
        })

        preview = self.mapping.preview_sync_prerequisites()
        bank_rows = [row for row in preview["planned"] if row["type"] == "bank"]
        self.assertEqual(len(bank_rows), 1)
        self.assertEqual(bank_rows[0]["default_account_id"], bank.id)

        self.mapping.prepare_sync_prerequisites(human_confirmed=True)
        journal = self.env["account.journal"].search([
            ("company_id", "=", self.company.id),
            ("type", "=", "bank"),
            ("default_account_id", "=", bank.id),
        ])
        self.assertEqual(len(journal), 1)

    def test_commit_returns_a_resolvable_audit_reference(self):
        result = self.mapping.prepare_sync_prerequisites(human_confirmed=True)
        self.assertEqual(result["audit_model"], "qbo.sync.log")
        log = self.env["qbo.sync.log"].browse(int(result["audit_ref"]))
        self.assertTrue(log.exists())
        self.assertEqual(log.entity_type, "mapping")
        self.assertEqual(log.operation, "create")
