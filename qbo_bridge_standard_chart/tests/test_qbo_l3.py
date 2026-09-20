from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.exceptions import UserError


@tagged("post_install", "-at_install")
class TestL3MasterChart(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Chart = self.env["account.chart.template"]
        self.Standard = self.env["qbo.standard.account"]
        self.Standard._ensure_l3_master_chart_imported()

    def _company(self):
        with patch.object(
            type(self.env["res.company"]), "_create_internal_project_task", lambda self: None
        ):
            return self.env["res.company"].create({"name": "L3 Test Co"})

    def test_l3_master_import_is_idempotent_and_preserves_layers(self):
        first = self.Standard.search_count(
            [("kernel_layer", "=", "L3"), ("entry_type", "=", "detail")]
        )
        self.Standard._ensure_l3_master_chart_imported()
        second = self.Standard.search_count(
            [("kernel_layer", "=", "L3"), ("entry_type", "=", "detail")]
        )
        self.assertGreater(first, 0)
        self.assertEqual(first, second)
        self.assertGreater(
            self.Standard.search_count(
                [("kernel_layer", "in", ("L0", "L1")), ("entry_type", "=", "detail")]
            ),
            0,
        )
        for code in self.Standard.search(
            [("kernel_layer", "=", "L3"), ("entry_type", "=", "detail")], limit=3
        ):
            self.assertEqual(code.kernel_layer, "L3")
            self.assertTrue(code.code)

    def test_l3_import_carries_yaml_enrichment_and_aopex_closure(self):
        # Existing L3 rows in the test DB predate the YAML enrichment fields;
        # re-import so this test validates the current artifact, not stale data.
        self.Standard.import_poseidon_l3_kernel()

        inventory = self.Standard.search(
            [("code", "=", "16000"), ("kernel_layer", "=", "L3")],
            limit=1,
        )
        self.assertTrue(inventory)
        self.assertEqual(inventory.activity_tag, "Trade/Retail / Manufacturing")

        rent = self.Standard.search(
            [("code", "=", "63010"), ("kernel_layer", "=", "L3")],
            limit=1,
        )
        self.assertTrue(rent)
        # The YAML descriptions were translated to English; this asserted the
        # old Portuguese prefix. Match on the account name, which is the part
        # the enrichment actually carries over, so a future copy edit does not
        # break the test again.
        self.assertIn("Rent Expense", rent.long_description or "")
        self.assertIn("L3 kernel status", rent.notes or "")
        self.assertIn("L3 seed-ready", rent.notes or "")
        self.assertIn("Aopex", (rent.dashboard_account_sets or "").split(","))

        aopex_codes = self.Chart._l3_batch_codes(
            self._company(),
            "dashboard_account_set",
            "Aopex",
        )
        self.assertIn("63010", aopex_codes)
        self.assertIn("68540", aopex_codes)

    def test_realign_pushes_the_declared_type_onto_published_accounts(self):
        # A tenant published under the DRAFT-era name heuristic carries the wrong
        # account_type forever: re-importing the master chart does not touch
        # company accounts, and activation only writes vals on create.
        company = self._company()
        land = self.Standard.search(
            [("code", "=", "15010"), ("kernel_layer", "=", "L3")], limit=1,
        )
        self.assertEqual(land.odoo_account_type, "asset_fixed")
        self.Chart.poseidon_activate_l3_accounts(company.id, ["15010"])
        account = self.env["account.account"].with_company(company).sudo().search(
            [("company_ids", "=", company.id), ("qbo_standard_account_id", "=", land.id)],
            limit=1,
        )
        self.assertTrue(account)
        account.write({"account_type": "asset_current"})

        stats = self.Standard.realign_l3_company_account_types()
        self.assertEqual(account.account_type, "asset_fixed")
        self.assertGreaterEqual(stats["updated"], 1)
        self.assertEqual(stats["blocked"], [])

        rerun = self.Standard.realign_l3_company_account_types()
        self.assertEqual(rerun["updated"], 0, "realignment must be idempotent")

    def test_realign_never_retypes_an_account_with_journal_items(self):
        company = self._company()
        # Two L3 accounts so the entry balances without depending on the L0/L1
        # chart being published into this throwaway company.
        self.Chart.poseidon_activate_l3_accounts(company.id, ["15010", "15020"])
        Account = self.env["account.account"].with_company(company).sudo()
        account = Account.search(
            [("company_ids", "=", company.id), ("code", "=", "15010")], limit=1,
        )
        counterpart = Account.search(
            [("company_ids", "=", company.id), ("code", "=", "15020")], limit=1,
        )
        self.assertTrue(account and counterpart)
        account.write({"account_type": "asset_current"})
        Journal = self.env["account.journal"].with_company(company).sudo()
        journal = Journal.search(
            [("company_id", "=", company.id), ("type", "=", "general")], limit=1,
        ) or Journal.create(
            {
                "name": "L3 Realign Test",
                "code": "L3RT",
                "type": "general",
                "company_id": company.id,
            },
        )
        self.env["account.move"].with_company(company).sudo().create(
            {
                "journal_id": journal.id,
                "line_ids": [
                    (0, 0, {"account_id": account.id, "debit": 100.0, "credit": 0.0}),
                    (0, 0, {"account_id": counterpart.id, "debit": 0.0, "credit": 100.0}),
                ],
            },
        )

        stats = self.Standard.realign_l3_company_account_types()
        self.assertEqual(
            account.account_type,
            "asset_current",
            "retyping a posted account moves its entries between statement lines",
        )
        self.assertIn(("15010", "asset_current", "asset_fixed"), stats["blocked"])

    def test_l3_activation_requires_existing_master(self):
        company = self._company()
        with self.assertRaises(UserError):
            self.Chart.poseidon_activate_l3_accounts(company.id, ["99999"])

    def test_l3_never_published_by_bulk_sync(self):
        company = self._company()
        self.Chart.poseidon_publish_missing_standard_accounts(company.id)
        l3_company_accounts = self.env["account.account"].with_company(company).sudo().search_count(
            [
                ("company_ids", "=", company.id),
                ("qbo_standard_account_id.kernel_layer", "=", "L3"),
            ]
        )
        self.assertEqual(l3_company_accounts, 0)

    def test_activate_l3_individual_and_idempotent(self):
        company = self._company()
        codes = self.Standard.search(
            [("kernel_layer", "=", "L3"), ("entry_type", "=", "detail")],
            order="code",
            limit=2,
        ).mapped("code")

        result = self.Chart.poseidon_activate_l3_accounts(company.id, codes)
        self.assertEqual(result["activated"], len(codes))
        self.assertEqual(len(result["accounts"]), len(codes))
        account_codes = [
            account["account"]["code"]
            for account in result["accounts"]
            if account.get("account", {}).get("code")
        ]
        self.assertEqual(sorted(account_codes), sorted(codes))

        for account in (
            self.env["account.account"].with_company(company).sudo().search(
                [("company_ids", "=", company.id), ("code", "in", codes)]
            )
        ):
            self.assertTrue(account.active)
            self.assertEqual(account.qbo_standard_account_id.kernel_layer, "L3")

        re_run = self.Chart.poseidon_activate_l3_accounts(company.id, codes)
        self.assertEqual(re_run["activated"], len(codes))
        for account in re_run["accounts"]:
            self.assertIn(account["action"], ("already_present", "linked"))

    def test_l3_batch_by_activity_tag_preview_and_apply(self):
        company = self._company()
        preview = self.Chart.poseidon_activate_l3_batch(
            company.id, "activity_tag", batch_key="Services", preview=True
        )
        self.assertTrue(preview["preview"])
        self.assertEqual(preview["count"], len(preview["codes"]))
        self.assertGreater(preview["count"], 0)
        self.assertEqual(preview["accounts"], [])
        for code in preview["codes"]:
            master = self.Standard.search(
                [
                    ("code", "=", code),
                    ("kernel_layer", "=", "L3"),
                    ("entry_type", "=", "detail"),
                ],
                limit=1,
            )
            self.assertTrue(master)
            self.assertTrue(master.activity_tag)

        applied = self.Chart.poseidon_activate_l3_batch(
            company.id, "activity_tag", batch_key="Services", preview=False
        )
        self.assertFalse(applied["preview"])
        self.assertEqual(applied["activated"], len(preview["codes"]))

    def test_l3_reversal_is_reversible(self):
        company = self._company()
        codes = self.Standard.search(
            [("kernel_layer", "=", "L3"), ("entry_type", "=", "detail")],
            order="code",
            limit=2,
        ).mapped("code")

        self.Chart.poseidon_activate_l3_accounts(company.id, codes)
        result = self.Chart.poseidon_deactivate_l3_accounts(company.id, codes)
        self.assertEqual(sorted(result["deactivated"]), sorted(codes))
        self.assertEqual(result["blocked"], [])
        self.assertEqual(result["deactivated_count"], len(codes))

        inactive = (
            self.env["account.account"]
            .with_company(company)
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("company_ids", "=", company.id),
                    ("code", "in", codes),
                    ("qbo_standard_account_id.kernel_layer", "=", "L3"),
                ]
            )
        )
        self.assertEqual(len(inactive), len(codes))
        for account in inactive:
            self.assertFalse(account.active)

    def test_unpublish_l3_deactivates_all(self):
        company = self._company()
        codes = self.Standard.search(
            [("kernel_layer", "=", "L3"), ("entry_type", "=", "detail")],
            order="code",
            limit=3,
        ).mapped("code")
        self.Chart.poseidon_activate_l3_accounts(company.id, codes)

        result = self.Chart.poseidon_unpublish_l3(company.id)
        self.assertGreaterEqual(len(result["deactivated"]), len(codes))


@tagged("post_install", "-at_install")
class TestL3KernelGovernance(TransactionCase):
    """Governance of the DERIVED kernel.

    L0/L1/L2 are the frozen kernel and are gated by version equality. L3 is an
    expansion of that kernel: it declares its own version (2025.3-L3.1), so
    equality can never hold and the artifact needs its own rules. These tests
    pin them, because the alternative — what shipped — was an importer that read
    whatever JSON sat at an operator-settable path and stamped its own status
    onto the record.
    """

    def setUp(self):
        super().setUp()
        self.Standard = self.env["qbo.standard.account"]
        self.Version = self.env["poseidon.kernel.version"]

    def _write_artifact(self, metadata):
        import json
        import tempfile
        from pathlib import Path

        path = Path(tempfile.mkdtemp()) / "l3.json"
        path.write_text(
            json.dumps({"metadata": metadata, "kernels": {"L3": {"accounts": []}}}),
            encoding="utf-8",
        )
        return path

    def test_refuses_an_l3_artifact_of_an_unknown_version(self):
        path = self._write_artifact(
            {"kernel_version": "9999.1-L3.0", "derived_from_kernel_version": "2025.3"}
        )
        with self.assertRaises(UserError) as caught:
            self.Standard.import_poseidon_l3_kernel(path=str(path))
        self.assertIn("unrecognised L3 expansion", str(caught.exception))

    def test_refuses_an_l3_derived_from_another_kernel(self):
        # The codes of such an expansion do not roll up into the L0/L1 accounts
        # this database carries.
        path = self._write_artifact(
            {"kernel_version": "2025.3-L3.1", "derived_from_kernel_version": "2024.1"}
        )
        with self.assertRaises(UserError) as caught:
            self.Standard.import_poseidon_l3_kernel(path=str(path))
        self.assertIn("derived from kernel", str(caught.exception))

    def test_a_missing_artifact_stays_lenient(self):
        # Absence means "L3 is unavailable here", not "the install is broken".
        stats = self.Standard.import_poseidon_l3_kernel(path="/nonexistent/l3.json")
        self.assertFalse(stats["available"])

    def test_the_recorded_status_is_the_artifact_status(self):
        version = self.Version.search(
            [("kernel_layer", "=", "L3")], limit=1
        )
        self.assertTrue(version, "seeding L3 must leave a version record")
        self.assertEqual(
            version.status,
            "frozen",
            "the artifact declares status FROZEN; the record must say what the "
            "database actually carries",
        )

    def test_the_importer_never_invents_a_status(self):
        # A future L3 revision that goes back to DRAFT must be recorded as one.
        self.assertEqual(self.Standard._kernel_status_from_metadata({"status": "DRAFT"}), "draft")
        self.assertEqual(self.Standard._kernel_status_from_metadata({"status": "FROZEN"}), "frozen")

    def test_a_draft_record_is_refreshed_when_its_artifact_freezes(self):
        # The artifact was revised in place (2025.3-L3.1 DRAFT -> FROZEN), so the
        # existing record has to follow. Deleting and recreating it is not an
        # option: company accounts FK-reference it through
        # account_account.poseidon_kernel_version_id.
        version = self.Version.search([("kernel_layer", "=", "L3")], limit=1)
        self.assertTrue(version)
        # Raw SQL: a frozen record refuses status writes through the ORM, which is
        # exactly the state a database upgraded from the draft is NOT in.
        self.env.cr.execute(
            "UPDATE poseidon_kernel_version SET status='draft', checksum='stale' "
            "WHERE id=%s",
            (version.id,),
        )
        self.env.invalidate_all()
        self.assertEqual(version.status, "draft")

        self.Standard.import_poseidon_l3_kernel()

        self.assertEqual(version.status, "frozen", "the record must track its artifact")
        self.assertNotEqual(version.checksum, "stale")
        self.assertEqual(
            self.Version.search_count(
                [("kernel_version", "=", "2025.3-L3.1"), ("kernel_layer", "=", "L3")],
            ),
            1,
            "refresh in place, never a second record",
        )

    def test_a_frozen_record_is_never_rewritten_by_a_reimport(self):
        version = self.Version.search(
            [("kernel_version", "=", "2025.3"), ("kernel_layer", "=", "L1")], limit=1,
        )
        self.assertTrue(version)
        self.assertEqual(version.status, "frozen")
        before = version.checksum
        self.Standard.import_bundled_chart()
        self.assertEqual(version.checksum, before)

    def test_l3_is_never_reported_as_the_installed_kernel(self):
        # The resolver falls back to the newest active record of any layer, so a
        # database without its operational-layer record would otherwise answer
        # "installed: 2025.3-L3.1" to every kernel gate — a version no gate
        # accepts, so the dashboard would withhold metrics. L3 is excluded by
        # LAYER, which is what keeps this true now that the artifact is frozen.
        self.Version.search([("kernel_layer", "!=", "L3")]).write({"active": False})
        status = self.Version.get_installed_kernel_status()
        self.assertNotEqual(status.get("kernel_version"), "2025.3-L3.1")
        self.assertFalse(status.get("installed"))

    def test_a_frozen_record_stays_supersedable(self):
        # Frozen records are immutable, but `active` is not protected — its own
        # successor must still be able to retire it.
        version = self.Version.search([("kernel_layer", "=", "L3")], limit=1)
        version.write({"active": False})
        self.assertFalse(version.active)
        with self.assertRaises(UserError):
            version.write({"checksum": "tampered"})
