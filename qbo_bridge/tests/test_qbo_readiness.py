"""Readiness diagnosis tests (ClickUp 86bb6vcpf, Phase 1).

TransactionCase because readiness reads journals, accounts and conflicts
through the ORM. No QBO network access is involved — readiness never calls the
API client.

Run:
    docker exec usgaap-backend bash -lc 'odoo --addons-path="$ADDONS_PATH" \
      --db_host=$POSTGRES_HOST --db_user=$POSTGRES_USER \
      --db_password=$POSTGRES_PASSWORD --data-dir=/tmp/qbotest \
      --no-http --http-port=8899 --gevent-port=8898 -d qbotest_readiness \
      -i qbo_bridge --test-enable --test-tags qbo_bridge --stop-after-init'
"""
from odoo.tests.common import TransactionCase


class TestQboReadiness(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env["res.company"].create({"name": "Readiness Corp"})
        self.realm = self.env["qbo.realm"].create({
            "name": "Readiness Realm",
            "realm_id": "987654321",
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

    def _keys(self, result, group):
        severity = {"blockers": "block", "warnings": "warn", "reviews": "review"}[group]
        return {c["key"] for c in result["checks"] if c["severity"] == severity}

    def test_disconnected_realm_blocks(self):
        self.realm.write({"state": "draft", "refresh_token": False})
        result = self.mapping.get_sync_readiness()
        self.assertEqual(result["state"], "blocked")
        self.assertIn("realm_connected", self._keys(result, "blockers"))
        self.assertIn("refresh_token", self._keys(result, "blockers"))

    def test_upload_mode_blocks_live_pull(self):
        self.realm.sync_mode = "upload"
        result = self.mapping.get_sync_readiness()
        self.assertIn("sync_mode", self._keys(result, "blockers"))

    def test_missing_sales_journal_blocks_when_invoices_enabled(self):
        self.env["account.journal"].search(
            [("company_id", "=", self.company.id), ("type", "=", "sale")]
        ).unlink()
        result = self.mapping.get_sync_readiness()
        self.assertIn("journal_sale", self._keys(result, "blockers"))
        self.assertIn("journal_sale", result["auto_actions"])

    def test_missing_sales_journal_only_warns_when_invoices_disabled(self):
        self.mapping.sync_invoices = False
        self.env["account.journal"].search(
            [("company_id", "=", self.company.id), ("type", "=", "sale")]
        ).unlink()
        result = self.mapping.get_sync_readiness()
        self.assertIn("journal_sale", self._keys(result, "warnings"))
        self.assertNotIn("journal_sale", self._keys(result, "blockers"))

    def test_payment_entity_is_reported_as_an_engine_gap(self):
        self.mapping.sync_payments = True
        result = self.mapping.get_sync_readiness()
        self.assertIn("engine_payment", self._keys(result, "warnings"))

    def test_imported_bank_account_without_journal_is_auto_preparable(self):
        self.env["account.account"].create({
            "name": "QBO Checking",
            "code": "10100",
            "account_type": "asset_cash",
            "company_ids": [(4, self.company.id)],
            "qbo_id": "QBO-BANK-1",
            "qbo_source_account_type": "Bank",
        })
        result = self.mapping.get_sync_readiness()
        self.assertIn("journal_bank", self._keys(result, "warnings"))
        self.assertIn("journal_bank", result["auto_actions"])

    def test_pending_conflicts_need_review(self):
        self.env["qbo.conflict"].create({
            "mapping_id": self.mapping.id,
            "entity_type": "account",
            "qbo_id": "QBO-1",
            "odoo_model": "account.account",
            "odoo_record_id": 1,
            "status": "pending",
        })
        result = self.mapping.get_sync_readiness()
        self.assertIn("conflicts_pending", self._keys(result, "reviews"))

    def test_readiness_never_writes_accounting_records(self):
        before = self.env["account.journal"].search_count(
            [("company_id", "=", self.company.id)]
        )
        self.mapping.get_sync_readiness()
        after = self.env["account.journal"].search_count(
            [("company_id", "=", self.company.id)]
        )
        self.assertEqual(before, after)

from odoo.exceptions import UserError


class TestQboOnboardingState(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env["res.company"].create({"name": "State Corp"})
        self.realm = self.env["qbo.realm"].create({
            "name": "State Realm",
            "realm_id": "111222333",
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

    def test_new_mapping_starts_at_not_started(self):
        self.assertEqual(self.mapping.onboarding_state, "not_started")

    def test_readiness_stamps_the_check_and_advances_the_state(self):
        self.mapping.get_sync_readiness()
        self.assertTrue(self.mapping.last_readiness_at)
        self.assertEqual(self.mapping.onboarding_state, "readiness_checked")

    def test_readiness_does_not_rewind_a_later_state(self):
        self.mapping.onboarding_state = "active"
        self.mapping.get_sync_readiness()
        self.assertEqual(self.mapping.onboarding_state, "active")

    def test_forbidden_transition_raises(self):
        for source, target in (
            ("not_started", "initial_sync_running"),
            ("qbo_connected", "active"),
            ("blocked", "initial_sync_running"),
            ("reauthorization_required", "initial_sync_running"),
        ):
            self.mapping.onboarding_state = source
            with self.assertRaises(UserError):
                self.mapping.onboarding_state = target
            self.assertEqual(self.mapping.onboarding_state, source)

    def test_allowed_transition_passes(self):
        self.mapping.onboarding_state = "setup_prepared"
        self.mapping.onboarding_state = "initial_sync_running"
        self.assertEqual(self.mapping.onboarding_state, "initial_sync_running")


class TestQboReadinessGates(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env["res.company"].create({"name": "Gate Corp"})
        self.realm = self.env["qbo.realm"].create({
            "name": "Gate Realm",
            "realm_id": "777888999",
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
        # Guarantee at least one blocker that is not the connection itself.
        self.env["account.journal"].search(
            [("company_id", "=", self.company.id), ("type", "=", "sale")]
        ).unlink()

    def test_request_pull_refuses_a_blocked_mapping(self):
        with self.assertRaises(UserError) as caught:
            self.mapping.action_request_pull()
        self.assertIn("sales journal", str(caught.exception))
        self.assertFalse(self.mapping.sync_requested)

    def test_cron_skips_a_blocked_mapping_and_logs_it(self):
        self.mapping.sync_requested = True
        self.env["qbo.realm"].cron_sync_all_realms()
        self.assertFalse(self.mapping.sync_requested)
        log = self.env["qbo.sync.log"].search(
            [("mapping_id", "=", self.mapping.id), ("entity_type", "=", "mapping")],
            order="id desc",
            limit=1,
        )
        self.assertEqual(log.operation, "skip")
        self.assertIn("sales journal", log.message)
