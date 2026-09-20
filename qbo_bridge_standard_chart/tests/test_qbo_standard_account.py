import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase

from ..models.qbo_standard_account import (
    CURRENT_KERNEL_FILENAME,
    CURRENT_KERNEL_VERSION,
)


class TestQboStandardAccount(TransactionCase):

    def test_import_chart_rows_creates_hierarchy(self):
        rows = [
            {
                "code": "10000",
                "description": "ASSET",
                "long_description": "Header",
                "type": "Header",
                "category": "Asset",
                "fs_mapping": "Balance Sheet",
                "parent_code": "",
                "normal_balance": "Debit",
                "tags": "",
                "default_vendors": "",
                "regulatory_mapping": "",
                "start_date": "",
                "end_date": "",
                "notes": "",
                "subcategory": "",
                "cash_flow_classification": "",
                "cost_center": "",
                "GAAP_classification": "Asset",
                "detailed_description": "Header",
            },
            {
                "code": "10100",
                "description": "Operating Cash",
                "long_description": "Detail",
                "type": "Detail",
                "category": "Asset",
                "fs_mapping": "Balance Sheet",
                "parent_code": "10000",
                "normal_balance": "Debit",
                "tags": "cash",
                "default_vendors": "",
                "regulatory_mapping": "",
                "start_date": "",
                "end_date": "",
                "notes": "",
                "subcategory": "Current Asset - Cash and Cash Equivalents",
                "cash_flow_classification": "Operating Activities",
                "cost_center": "CC-1000",
                "GAAP_classification": "US-GAAP Asset",
                "detailed_description": "Cash account",
            },
        ]
        stats = self.env["qbo.standard.account"].import_chart_rows(rows)

        header = self.env["qbo.standard.account"].search(
            [("code", "=", "10000"), ("entry_type", "=", "header")],
        )
        detail = self.env["qbo.standard.account"].search(
            [("code", "=", "10100"), ("entry_type", "=", "detail")],
        )
        # The module seeds the master chart at install time, so kernel codes
        # (e.g. the 10100 detail) may already exist and be *updated* rather than
        # created. Assert the rows this test introduced are present and correctly
        # linked instead of pinning a global create count, which is brittle
        # against a pre-seeded chart.
        self.assertTrue(header, "header row must be imported")
        self.assertTrue(detail, "detail row must be imported")
        self.assertGreaterEqual(stats["created"] + stats["updated"], 2)
        self.assertEqual(detail.parent_id, header)
        self.assertEqual(detail.odoo_account_type, "asset_cash")

    def test_bridge_rule_inherits_standard_account_fields(self):
        # 94010 is a fixture-only code: 40010 is owned by the seeded L3 kernel.
        standard = self.env["qbo.standard.account"].create(
            {
                "code": "94010",
                "description": "Consulting Revenue",
                "entry_type": "detail",
                "category": "Revenue",
                "normal_balance": "credit",
                "odoo_account_type": "income",
            },
        )
        rule = self.env["qbo.account.bridge.rule"].create(
            {
                "standard_account_id": standard.id,
                "match_name": "Consulting Income",
                "canonical_code": "temp",
                "canonical_name": "temp",
                "canonical_account_type": "income",
            },
        )

        self.assertEqual(rule.canonical_code, "94010")
        self.assertEqual(rule.canonical_name, "Consulting Revenue")
        self.assertEqual(rule.canonical_account_type, "income")

    def test_sync_wizard_creates_company_account_and_pushes(self):
        # 96110 is a fixture-only code: 61010 is owned by the seeded L3 kernel.
        standard = self.env["qbo.standard.account"].create(
            {
                "code": "96110",
                "description": "Office Supplies",
                "entry_type": "detail",
                "category": "Expense",
                "normal_balance": "debit",
                "odoo_account_type": "expense",
            },
        )
        realm = self.env["qbo.realm"].create(
            {
                "name": "Realm",
                "realm_id": "123",
                "client_id": "cid",
                "client_secret": "secret",
                "state": "connected",
            },
        )
        mapping = self.env["qbo.company.mapping"].create(
            {
                "company_id": self.env.company.id,
                "realm_id": realm.id,
            },
        )
        wizard = self.env["qbo.standard.account.sync.wizard"].create(
            {
                "standard_account_id": standard.id,
                "mapping_ids": [(6, 0, [mapping.id])],
            },
        )

        with patch(
            "odoo.addons.qbo_bridge.services.qbo_sync_engine.QBOSyncEngine.push_account_record",
        ) as push_account:
            wizard.action_sync()

        account = self.env["account.account"].search(
            [
                ("company_ids", "=", self.env.company.id),
                ("qbo_standard_account_id", "=", standard.id),
            ],
            limit=1,
        )
        self.assertTrue(account)
        self.assertEqual(account.code, "96110")
        self.assertEqual(account.name, "Office Supplies")
        push_account.assert_called_once()

    def test_sync_detail_accounts_to_company_creates_native_chart_accounts(self):
        # 91210 is a fixture-only code: 12010 is owned by the seeded L3 kernel.
        standard = self.env["qbo.standard.account"].create(
            {
                "code": "91210",
                "description": "Accounts Receivable",
                "entry_type": "detail",
                "category": "Asset",
                "normal_balance": "debit",
                "odoo_account_type": "asset_receivable",
            },
        )

        stats = self.env["qbo.standard.account"].sync_detail_accounts_to_company(
            self.env.company,
            update_existing=True,
        )

        account = self.env["account.account"].search(
            [
                ("company_ids", "=", self.env.company.id),
                ("qbo_standard_account_id", "=", standard.id),
            ],
            limit=1,
        )
        # A fresh install seeds the master chart, so sync_detail_accounts_to_company
        # also materializes the seeded kernel detail accounts. Assert the company
        # account for the code this test introduced was created instead of pinning
        # a global create count.
        self.assertTrue(account)
        self.assertGreaterEqual(stats["created"], 1)
        self.assertEqual(account.name, "Accounts Receivable")

    def test_header_child_count_updates_from_parent_link(self):
        # 97000/97010 are fixture-only codes: 70000/70010 are owned by the
        # seeded L3 kernel.
        header = self.env["qbo.standard.account"].create(
            {
                "code": "97000",
                "description": "OTHER HEADER",
                "entry_type": "header",
                "category": "Other",
                "normal_balance": "credit",
                "odoo_account_type": "income_other",
            },
        )
        detail = self.env["qbo.standard.account"].create(
            {
                "code": "97010",
                "description": "Other Income",
                "entry_type": "detail",
                "category": "Other",
                "normal_balance": "credit",
                "odoo_account_type": "income_other",
                "parent_id": header.id,
            },
        )

        self.assertEqual(header.child_count, 1)
        self.assertEqual(detail.parent_id, header)

    def test_header_cannot_have_parent(self):
        header = self.env["qbo.standard.account"].create(
            {
                "code": "71000",
                "description": "ROOT HEADER",
                "entry_type": "header",
                "category": "Asset",
                "normal_balance": "debit",
                "odoo_account_type": "asset_current",
            },
        )

        with self.assertRaises(ValidationError):
            self.env["qbo.standard.account"].create(
                {
                    "code": "71001",
                    "description": "INVALID HEADER",
                    "entry_type": "header",
                    "category": "Asset",
                    "normal_balance": "debit",
                    "odoo_account_type": "asset_current",
                    "parent_id": header.id,
                },
            )

    def test_poseidon_activate_standard_account(self):
        standard = self.env["qbo.standard.account"].create(
            {
                "code": "52010",
                "description": "Marketing Expense",
                "entry_type": "detail",
                "category": "Expense",
                "normal_balance": "debit",
                "odoo_account_type": "expense",
            },
        )

        # Test activation (creates account)
        result = self.env["account.chart.template"].poseidon_activate_standard_account_for_company(
            self.env.company.id,
            standard_account_id=standard.id,
        )
        self.assertEqual(result["action"], "created")
        self.assertEqual(result["account"]["code"], "52010")

        # Test activation again (already present)
        result_again = self.env["account.chart.template"].poseidon_activate_standard_account_for_company(
            self.env.company.id,
            standard_account_id=standard.id,
        )
        self.assertEqual(result_again["action"], "already_present")

    def _make_detail_standard(self, code, description):
        return self.env["qbo.standard.account"].create(
            {
                "code": code,
                "description": description,
                "entry_type": "detail",
                "category": "Expense",
                "normal_balance": "debit",
                "odoo_account_type": "expense",
            },
        )

    def test_sync_does_not_update_a_locked_company_account(self):
        if "poseidon_kernel_locked" not in self.env["account.account"]._fields:
            self.skipTest("poseidon_accounting_kernel not installed; lock field absent")

        standard = self._make_detail_standard("96310", "Bank Fees")
        # First sync creates the company account.
        self.env["qbo.standard.account"].sync_detail_accounts_to_company(
            self.env.company, update_existing=True
        )
        account = self.env["account.account"].search(
            [
                ("company_ids", "=", self.env.company.id),
                ("qbo_standard_account_id", "=", standard.id),
            ],
            limit=1,
        )
        self.assertTrue(account)
        # Lock the account, then rename the master so an unguarded sync WOULD
        # rewrite it.
        account.with_context(poseidon_kernel_skip_lock_check=True).write(
            {"poseidon_kernel_locked": True}
        )
        standard.description = "Bank Fees (renamed)"

        stats = self.env["qbo.standard.account"].sync_detail_accounts_to_company(
            self.env.company, update_existing=True
        )

        self.assertEqual(stats["blocked"], 1)
        self.assertEqual(stats["updated"], 0)
        account.invalidate_recordset(["name"])
        self.assertEqual(account.name, "Bank Fees", "locked account must not be refreshed")

    def test_sync_updates_an_unlocked_existing_account(self):
        if "poseidon_kernel_locked" not in self.env["account.account"]._fields:
            self.skipTest("poseidon_accounting_kernel not installed; lock field absent")

        standard = self._make_detail_standard("96320", "Merchant Fees")
        self.env["qbo.standard.account"].sync_detail_accounts_to_company(
            self.env.company, update_existing=True
        )
        standard.description = "Merchant Fees (updated)"

        stats = self.env["qbo.standard.account"].sync_detail_accounts_to_company(
            self.env.company, update_existing=True
        )

        account = self.env["account.account"].search(
            [
                ("company_ids", "=", self.env.company.id),
                ("qbo_standard_account_id", "=", standard.id),
            ],
            limit=1,
        )
        self.assertEqual(stats["blocked"], 0)
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(account.name, "Merchant Fees (updated)")

    def test_sync_still_creates_missing_accounts(self):
        self._make_detail_standard("96330", "Wire Fees")

        stats = self.env["qbo.standard.account"].sync_detail_accounts_to_company(
            self.env.company, update_existing=True
        )

        self.assertGreaterEqual(stats["created"], 1)
        self.assertEqual(stats["blocked"], 0)

    def test_poseidon_publish_missing_standard_accounts(self):
        standard = self.env["qbo.standard.account"].create(
            {
                "code": "52020",
                "description": "Advertising Expense",
                "entry_type": "detail",
                "category": "Expense",
                "normal_balance": "debit",
                "odoo_account_type": "expense",
            },
        )

        # Publish
        res = self.env["account.chart.template"].poseidon_publish_missing_standard_accounts(
            self.env.company.id,
            update_existing=True,
        )
        self.assertEqual(res["company_id"], self.env.company.id)
        self.assertTrue(res["stats"]["created"] >= 1)

    # --- Kernel discovery / versioning / required-L0 (US GAAP route) ----------

    def _kernel_model(self):
        return self.env["qbo.standard.account"]

    def test_discovers_current_kernel_filename(self):
        path = self._kernel_model()._default_kernel_json_path()
        if not path:
            self.skipTest("kernel_v2025.3.json not reachable from the addon path")
        self.assertEqual(Path(path).name, CURRENT_KERNEL_FILENAME)

    def test_imports_current_kernel_marks_required_l0(self):
        model = self._kernel_model()
        path = model._default_kernel_json_path()
        if not path:
            self.skipTest("kernel_v2025.3.json not reachable from the addon path")

        model.import_poseidon_kernel_json(kernel_path=path, layer="L0")
        account = model.search(
            [("code", "=", "10000"), ("kernel_layer", "=", "L0")], limit=1
        )
        self.assertTrue(account)
        self.assertEqual(account.kernel_version, CURRENT_KERNEL_VERSION)
        self.assertTrue(account.kernel_required, "required L0 account must be flagged")

    def test_default_l1_import_preserves_required_l0(self):
        """Required=true lives on L0; an L1 default import must still flag it."""
        model = self._kernel_model()
        path = model._default_kernel_json_path()
        if not path:
            self.skipTest("kernel_v2025.3.json not reachable from the addon path")

        model.import_poseidon_kernel_json(kernel_path=path, layer="L1")
        # 10000 is an L0-required code present in L1: it must carry kernel_required
        # even though the L1 row itself has no `required` flag.
        account = model.search(
            [("code", "=", "10000"), ("kernel_layer", "=", "L1")], limit=1
        )
        self.assertTrue(account)
        self.assertTrue(
            account.kernel_required,
            "required L0 semantics must survive an L1 default import",
        )
        # A non-required L1-only sub-account stays unflagged.
        rent = model.search(
            [("code", "=", "63000"), ("kernel_layer", "=", "L1")], limit=1
        )
        self.assertTrue(rent)
        self.assertFalse(rent.kernel_required)

    def test_missing_kernel_is_blocking(self):
        model = self._kernel_model()
        with patch.object(
            type(model), "_default_kernel_json_path", return_value=None
        ):
            with self.assertRaises(UserError):
                model._require_kernel_json_path()
            with self.assertRaises(UserError):
                model.import_bundled_chart()

    def test_rejects_non_current_kernel_version(self):
        model = self._kernel_model()
        payload = {
            "metadata": {"kernel_version": "2025.2", "gaap_basis": "US_GAAP"},
            "kernels": {
                "L0": {"accounts": [
                    {"layer": "L0", "code": "10000", "name": "Operating Cash",
                     "category": "ASSET", "required": True, "normal_balance": "Debit"},
                ]},
            },
        }
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as handle:
            json.dump(payload, handle)
            tmp_path = handle.name
        try:
            with self.assertRaises(UserError):
                model.import_poseidon_kernel_json(kernel_path=tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_ensure_master_chart_imported_idempotent(self):
        Std = self.env["qbo.standard.account"]
        Std._ensure_master_chart_imported()
        first = Std.with_context(active_test=False).search_count(
            [("entry_type", "=", "detail"), ("kernel_layer", "!=", False)]
        )
        Std._ensure_master_chart_imported()
        second = Std.with_context(active_test=False).search_count(
            [("entry_type", "=", "detail"), ("kernel_layer", "!=", False)]
        )
        self.assertTrue(first, "master chart should be imported")
        self.assertEqual(first, second, "second import must be a no-op")

    def test_sync_required_only_publishes_l0_subset(self):
        Std = self.env["qbo.standard.account"]
        Std._ensure_master_chart_imported()
        # Fresh company without the internal project: res.company.create in this
        # build triggers _create_internal_project_task, which trips a pre-existing
        # sale_timesheet project_project.billing_type not-null bug. The project
        # is irrelevant to the publish-under-test, so skip it.
        with patch.object(
            type(self.env["res.company"]), "_create_internal_project_task", lambda self: None
        ):
            company = self.env["res.company"].create({"name": "L0 Only Co"})
        Std.sync_detail_accounts_to_company(company, required_only=True)
        Account = self.env["account.account"].with_context(active_test=False)
        seeded = Account.search(
            [("company_ids", "=", company.id), ("qbo_standard_account_id", "!=", False)]
        )
        expected = Std.search(
            [("entry_type", "=", "detail"), ("kernel_required", "=", True)]
        )
        self.assertTrue(seeded, "required-only publish must create accounts")
        self.assertEqual(set(seeded.with_company(company).mapped("code")), set(expected.mapped("code")))
        if "poseidon_master_account_id" in self.env["account.account"]._fields:
            self.assertTrue(all(a.poseidon_master_account_id for a in seeded))
