"""Integration tests for QBOSyncEngine.

Uses Odoo TransactionCase so models are available, but mocks QBOApiClient
so no live QBO credentials are required.

Run:
    ./odoo-bin -c deploy/odoo/kodoo.dev-host.local.conf -d ktest \
        --test-enable -i qbo_bridge --stop-after-init
"""
from unittest.mock import MagicMock, patch
import zipfile
from io import BytesIO

from odoo import fields
from odoo.tests.common import TransactionCase, tagged


class TestQBOSyncEngineAccounts(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env["res.company"].create({
            "name": "Test Umbrella Corp",
        })
        self.realm = self.env["qbo.realm"].create({
            "name": "Test QBO Realm",
            "realm_id": "123456789",
            "client_id": "test_client_id",
            "client_secret": "test_client_secret",
            "state": "connected",
            "sync_mode": "pull_only",
            "refresh_token": "rt",
        })
        self.mapping = self.env["qbo.company.mapping"].create({
            "company_id": self.company.id,
            "realm_id": self.realm.id,
            "sync_accounts": True,
            "sync_partners": False,
            "sync_invoices": False,
            "sync_payments": False,
            "sync_journal_entries": False,
            "sync_products": False,
        })

    def _make_engine(self):
        from ..services.qbo_sync_engine import QBOSyncEngine
        engine = QBOSyncEngine(self.env, self.mapping)
        engine.client = MagicMock()
        return engine

    def test_pull_new_account_creates_odoo_record(self):
        engine = self._make_engine()
        qbo_accounts = [
            {
                "Id": "QBO-ACC-001",
                "SyncToken": "0",
                "Name": "Operating Cash",
                "AccountType": "Bank",
                "Active": True,
                "MetaData": {"LastUpdatedTime": "2024-01-01T00:00:00-00:00"},
            },
        ]
        engine._upsert_accounts(qbo_accounts, direction="pull")

        acc = self.env["account.account"].with_company(self.company).search([
            ("qbo_id", "=", "QBO-ACC-001"),
            ("company_ids", "=", self.company.id),
        ])
        self.assertTrue(acc, "Expected account.account to be created from QBO pull")
        self.assertEqual(acc.name, "Operating Cash")

    def test_pull_existing_account_updates_name(self):
        # Pre-create account with qbo_id
        existing = self.env["account.account"].with_company(self.company).create({
            "name": "Old Name",
            "code": "1001",
            "account_type": "asset_cash",
            "qbo_id": "QBO-ACC-002",
            "company_ids": [(4, self.company.id)],
        })
        engine = self._make_engine()
        qbo_accounts = [
            {
                "Id": "QBO-ACC-002",
                "SyncToken": "1",
                "Name": "Updated Cash Name",
                "AccountType": "Bank",
                "Active": True,
                "MetaData": {"LastUpdatedTime": "2024-01-01T00:00:00-00:00"},
            },
        ]
        engine._upsert_accounts(qbo_accounts, direction="pull")
        self.assertEqual(existing.name, "Updated Cash Name")

    def test_conflict_detected_creates_conflict_record(self):
        """When both sides changed since last sync, a conflict is created."""
        import datetime

        last_sync = datetime.datetime(2024, 1, 1, 0, 0, 0)
        self.mapping.write({"last_sync_accounts": last_sync})

        # Pre-create account modified AFTER last_sync
        existing = self.env["account.account"].with_company(self.company).create({
            "name": "Disputed Account",
            "code": "1002",
            "account_type": "asset_cash",
            "qbo_id": "QBO-ACC-003",
            "company_ids": [(4, self.company.id)],
        })
        # Force write_date > last_sync
        self.env.cr.execute(
            "UPDATE account_account SET write_date = %s WHERE id = %s",
            (datetime.datetime(2024, 6, 1), existing.id),
        )

        engine = self._make_engine()
        qbo_accounts = [
            {
                "Id": "QBO-ACC-003",
                "SyncToken": "2",
                "Name": "QBO Changed Name",
                "AccountType": "Bank",
                "Active": True,
                # QBO also modified after last_sync
                "MetaData": {"LastUpdatedTime": "2024-06-02T00:00:00-00:00"},
            },
        ]
        engine._upsert_accounts(qbo_accounts, direction="pull")

        conflict = self.env["qbo.conflict"].search([
            ("qbo_id", "=", "QBO-ACC-003"),
            ("mapping_id", "=", self.mapping.id),
        ])
        self.assertTrue(conflict, "Expected a conflict record to be created")
        self.assertEqual(conflict.status, "pending")

    def test_sync_log_written_on_success(self):
        engine = self._make_engine()
        qbo_accounts = [
            {
                "Id": "QBO-ACC-004",
                "SyncToken": "0",
                "Name": "Log Test Account",
                "AccountType": "Expense",
                "Active": True,
                "MetaData": {"LastUpdatedTime": "2024-01-01T00:00:00-00:00"},
            },
        ]
        engine._upsert_accounts(qbo_accounts, direction="pull")

        log = self.env["qbo.sync.log"].search([
            ("qbo_id", "=", "QBO-ACC-004"),
            ("mapping_id", "=", self.mapping.id),
        ])
        self.assertTrue(log)
        self.assertEqual(log.status, "success")
        self.assertEqual(log.direction, "pull")

    def test_sync_receives_company_configuration_snapshot(self):
        engine = self._make_engine()
        snapshot = {
            "company_info": {"LegalName": "Test Umbrella Corp"},
            "preferences": {"AccountingInfoPrefs": {"FirstMonthOfFiscalYear": "January"}},
            "bank_accounts": [],
        }
        engine.client.get_configuration.return_value = snapshot

        engine._sync_configuration()

        self.assertEqual(self.mapping.configuration_snapshot, snapshot)
        self.assertTrue(self.mapping.configuration_synced_at)
        log = self.env["qbo.sync.log"].search(
            [("mapping_id", "=", self.mapping.id), ("entity_type", "=", "mapping")],
            limit=1,
        )
        self.assertEqual(log.operation, "update")

    def test_push_new_account_calls_api(self):
        engine = self._make_engine()
        engine.client.create_account.return_value = {"Id": "QBO-NEW-001", "SyncToken": "0"}

        new_acc = self.env["account.account"].with_company(self.company).create({
            "name": "New Kodoo Account",
            "code": "9001",
            "account_type": "expense",
            "company_ids": [(4, self.company.id)],
            # No qbo_id — will be pushed
        })
        engine._push_accounts()

        engine.client.create_account.assert_called_once()
        self.assertEqual(new_acc.qbo_id, "QBO-NEW-001")

    def test_pull_account_links_rule_without_replacing_source_identity(self):
        rule = self.env["qbo.account.bridge.rule"].create({
            "match_account_type": "Bank",
            "match_account_subtype": "Checking",
            "canonical_code": "110000",
            "canonical_name": "Main Operating Cash",
            "canonical_account_type": "asset_cash",
        })
        engine = self._make_engine()

        engine._upsert_accounts([
            {
                "Id": "QBO-ACC-005",
                "SyncToken": "0",
                "Name": "Checking - North Division",
                "AcctNum": "1001",
                "AccountType": "Bank",
                "AccountSubType": "Checking",
                "Active": True,
                "MetaData": {"LastUpdatedTime": "2024-01-01T00:00:00-00:00"},
            },
        ], direction="pull")

        acc = self.env["account.account"].with_company(self.company).search([
            ("qbo_id", "=", "QBO-ACC-005"),
            ("company_ids", "=", self.company.id),
        ])
        self.assertTrue(acc)
        self.assertEqual(acc.code, "1001")
        self.assertEqual(acc.name, "Checking - North Division")
        self.assertEqual(acc.qbo_bridge_rule_id, rule)
        self.assertEqual(acc.qbo_source_name, "Checking - North Division")
        self.assertEqual(acc.qbo_source_account_number, "1001")

    def test_pull_account_keeps_existing_canonical_account_separate(self):
        rule = self.env["qbo.account.bridge.rule"].create({
            "match_name": "Payroll Clearing",
            "canonical_code": "210500",
            "canonical_name": "Payroll Clearing",
            "canonical_account_type": "liability_current",
        })
        existing = self.env["account.account"].with_company(self.company).create({
            "name": "Payroll Clearing",
            "code": "210500",
            "account_type": "liability_current",
            "company_ids": [(4, self.company.id)],
        })
        engine = self._make_engine()

        engine._upsert_accounts([
            {
                "Id": "QBO-ACC-006",
                "SyncToken": "0",
                "Name": "Payroll Clearing",
                "AcctNum": "210500",
                "AccountType": "Other Current Liability",
                "Active": True,
                "MetaData": {"LastUpdatedTime": "2024-01-01T00:00:00-00:00"},
            },
        ], direction="pull")

        source = self.env["account.account"].with_company(self.company).search([
            ("company_ids", "=", self.company.id),
            ("qbo_id", "=", "QBO-ACC-006"),
        ])
        canonical = self.env["account.account"].with_company(self.company).search([
            ("company_ids", "=", self.company.id),
            ("code", "=", "210500"),
        ])
        self.assertEqual(canonical, existing)
        self.assertFalse(canonical.qbo_id)
        self.assertEqual(source.name, "Payroll Clearing")
        self.assertEqual(source.code, "QBO.QBOACC006")
        self.assertEqual(source.qbo_bridge_rule_id, rule)


@tagged("post_install", "-at_install")
class TestQBOCanonicalDestination(TransactionCase):

    def setUp(self):
        super().setUp()
        if "poseidon.mapping.decision" not in self.env:
            self.skipTest("mapping governance is not installed")
        self.company = self.env["res.company"].create({"name": "Canonical QBO Co"})
        realm = self.env["qbo.realm"].create({
            "name": "Canonical Realm",
            "realm_id": "canonical-realm",
            "client_id": "cid",
            "client_secret": "secret",
        })
        self.mapping = self.env["qbo.company.mapping"].create({
            "company_id": self.company.id,
            "realm_id": realm.id,
        })
        self.source = self.env["account.account"].with_company(self.company).create({
            "name": "QBO Checking",
            "code": "10100",
            "account_type": "asset_cash",
            "qbo_id": "QBO-BANK-CANONICAL",
            "qbo_source_account_type": "Bank",
            "company_ids": [(4, self.company.id)],
        })
        self.destination = self.env["account.account"].with_company(self.company).create({
            "name": "Operating Cash",
            "code": "10000",
            "account_type": "asset_cash",
            "company_ids": [(4, self.company.id)],
        })
        rule = self.env["qbo.account.bridge.rule"].create({
            "match_account_type": "Bank",
            "canonical_code": "10000",
            "canonical_name": "Operating Cash",
            "canonical_account_type": "asset_cash",
        })
        self.env["poseidon.mapping.decision"].create({
            "company_id": self.company.id,
            "source_account_id": self.source.id,
            "destination_account_id": self.destination.id,
            "bridge_rule_id": rule.id,
            "qbo_id": self.source.qbo_id,
            "state": "confirmed",
        })

    def test_confirmed_mapping_is_the_operational_account(self):
        from ..services.qbo_readiness import qbo_bank_accounts
        from ..services.qbo_sync_engine import QBOSyncEngine

        engine = QBOSyncEngine(self.env, self.mapping)
        account = engine._resolve_qbo_account({"value": self.source.qbo_id})

        self.assertEqual(account, self.destination)
        self.assertEqual(qbo_bank_accounts(self.env, self.company), self.destination)


class TestQBOSyncEngineProducts(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.realm = self.env["qbo.realm"].create({
            "name": "Realm B",
            "realm_id": "987654321",
            "client_id": "cid",
            "client_secret": "csec",
            "state": "connected",
        })
        self.mapping = self.env["qbo.company.mapping"].create({
            "company_id": self.company.id,
            "realm_id": self.realm.id,
            "sync_products": True,
        })

    def _make_engine(self):
        from ..services.qbo_sync_engine import QBOSyncEngine
        engine = QBOSyncEngine(self.env, self.mapping)
        engine.client = MagicMock()
        return engine

    def test_pull_product_creates_template(self):
        engine = self._make_engine()
        engine._upsert_products([
            {
                "Id": "ITEM-001",
                "SyncToken": "0",
                "Name": "Widget Pro",
                "Type": "Inventory",
                "UnitPrice": 99.99,
                "PurchaseCost": 40.0,
                "Active": True,
                "MetaData": {"LastUpdatedTime": "2024-01-01T00:00:00-00:00"},
            },
        ])
        product = self.env["product.template"].search([("qbo_id", "=", "ITEM-001")])
        self.assertTrue(product)
        self.assertAlmostEqual(product.list_price, 99.99)

    def test_pull_product_wires_income_account_from_item_ref(self):
        income = self.env["account.account"].with_company(self.company).create({
            "name": "Services Revenue",
            "code": "41001",
            "account_type": "income",
            "qbo_id": "QBO-ACC-41001",
            "company_ids": [(6, 0, [self.company.id])],
        })
        engine = self._make_engine()
        engine._upsert_products([
            {
                "Id": "ITEM-002",
                "SyncToken": "0",
                "Name": "Study Visit",
                "Type": "Service",
                "IncomeAccountRef": {"value": "QBO-ACC-41001", "name": "Services Revenue"},
                "Active": True,
                "MetaData": {"LastUpdatedTime": "2024-01-01T00:00:00-00:00"},
            },
        ])
        product = self.env["product.template"].search([("qbo_id", "=", "ITEM-002")])
        self.assertEqual(product.property_account_income_id, income)


class TestQBOSyncEnginePackageImport(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.realm = self.env["qbo.realm"].create({
            "name": "Realm Package",
            "realm_id": "1122334455",
            "client_id": "cid",
            "client_secret": "csec",
            "state": "connected",
        })
        self.mapping = self.env["qbo.company.mapping"].create({
            "company_id": self.company.id,
            "realm_id": self.realm.id,
            "sync_products": False,
        })

    def _build_xlsx(self, headers, rows):
        try:
            import io

            import openpyxl
        except ImportError:
            self.skipTest("openpyxl not installed")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(headers)
        for row in rows:
            ws.append(row)
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def _build_package(self):
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("Balance_sheet.xlsx", self._build_xlsx(["Account", "Amount"], [["Cash", "100.00"]]))
            archive.writestr("Customers.xlsx", self._build_xlsx(["Customer", "Email", "Active"], [["Jane Doe", "jane@example.com", "true"]]))
            archive.writestr("Employees.xlsx", self._build_xlsx(["Name", "Email", "Active"], [["John Staff", "john@example.com", "true"]]))
            archive.writestr(
                "General_ledger.xlsx",
                self._build_xlsx(["TxnDate", "DocNumber", "Account", "Debit", "Credit", "Memo"], [["2026-05-11", "GL-1", "Cash", "100.00", "0.00", "Deposit"]]),
            )
            archive.writestr(
                "Journal.xlsx",
                self._build_xlsx(["TxnDate", "DocNumber", "Account", "Debit", "Credit", "Memo"], [["2026-05-11", "JRN-1", "Cash", "0.00", "100.00", "Revenue"]]),
            )
            archive.writestr("Profit_and_loss.xlsx", self._build_xlsx(["Account", "Amount"], [["Revenue", "100.00"]]))
            archive.writestr("Trial_balance.xlsx", self._build_xlsx(["Account", "Debit", "Credit"], [["Cash", "100.00", "0.00"]]))
            archive.writestr("Vendors.xlsx", self._build_xlsx(["Vendor", "Email", "Active"], [["Acme Corp", "ap@acme.example", "true"]]))
        return buf.getvalue()

    def test_sync_from_package_imports_contacts(self):
        from ..services.qbo_file_parser import QBOFileParser
        from ..services.qbo_sync_engine import QBOSyncEngine

        parser = QBOFileParser()
        package = parser.parse_package(self._build_package())
        engine = QBOSyncEngine(self.env, self.mapping)

        summary = engine.sync_from_package(package)

        self.assertEqual(summary["customers"], 1)
        self.assertEqual(summary["vendors"], 1)
        self.assertTrue(summary["warnings"])
        self.assertTrue(self.env["res.partner"].search([("name", "=", "Jane Doe")]))
        self.assertTrue(self.env["res.partner"].search([("name", "=", "Acme Corp")]))


class TestQBOSyncEngineInvoices(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env["res.company"].create({"name": "Historical QBO Co"})
        self.realm = self.env["qbo.realm"].create({
            "name": "Historical Realm",
            "realm_id": "historical-realm",
            "client_id": "cid",
            "client_secret": "secret",
        })
        self.mapping = self.env["qbo.company.mapping"].create({
            "company_id": self.company.id,
            "realm_id": self.realm.id,
        })
        self.income = self.env["account.account"].with_company(self.company).create({
            "name": "Historical Revenue",
            "code": "41991",
            "account_type": "income",
            "company_ids": [(6, 0, [self.company.id])],
        })
        self.expense = self.env["account.account"].with_company(self.company).create({
            "name": "Historical Expense",
            "code": "51991",
            "account_type": "expense",
            "qbo_id": "QBO-EXPENSE",
            "company_ids": [(6, 0, [self.company.id])],
        })
        receivable = self.env["account.account"].with_company(self.company).create({
            "name": "Historical Receivable",
            "code": "11991",
            "account_type": "asset_receivable",
            "reconcile": True,
            "company_ids": [(6, 0, [self.company.id])],
        })
        payable = self.env["account.account"].with_company(self.company).create({
            "name": "Historical Payable",
            "code": "21991",
            "account_type": "liability_payable",
            "reconcile": True,
            "company_ids": [(6, 0, [self.company.id])],
        })
        self.bank_account = self.env["account.account"].with_company(self.company).create({
            "name": "Historical Bank Account",
            "code": "11992",
            "account_type": "asset_cash",
            "qbo_id": "QBO-BANK",
            "company_ids": [(6, 0, [self.company.id])],
        })
        self.cash_account = self.env["account.account"].with_company(self.company).create({
            "name": "Historical Cash Account",
            "code": "11993",
            "account_type": "asset_cash",
            "qbo_id": "QBO-CASH",
            "company_ids": [(6, 0, [self.company.id])],
        })
        self.transfer_account = self.env["account.account"].with_company(self.company).create({
            "name": "Historical Clearing",
            "code": "11994",
            "account_type": "asset_current",
            "reconcile": True,
            "company_ids": [(6, 0, [self.company.id])],
        })
        self.company.transfer_account_id = self.transfer_account.id
        for journal_type, name, code in (
            ("sale", "Historical Sales", "HSL"),
            ("purchase", "Historical Bills", "HBL"),
            ("bank", "Historical Bank", "HBK"),
            ("cash", "Historical Cash", "HCS"),
            ("general", "Historical General", "HGN"),
        ):
            values = {
                "name": name,
                "code": code,
                "type": journal_type,
                "company_id": self.company.id,
            }
            if journal_type == "bank":
                values["default_account_id"] = self.bank_account.id
            elif journal_type == "cash":
                values["default_account_id"] = self.cash_account.id
            self.env["account.journal"].create(values)
        self.customer = self.env["res.partner"].create({
            "name": "Historical Customer",
            "qbo_id": "QBO-CUSTOMER",
            "company_id": self.company.id,
        })
        self.customer.with_company(self.company).property_account_receivable_id = receivable
        self.vendor = self.env["res.partner"].create({
            "name": "Historical Vendor",
            "qbo_id": "QBO-VENDOR",
            "company_id": self.company.id,
        })
        self.vendor.with_company(self.company).property_account_payable_id = payable
        self.product = self.env["product.template"].with_company(self.company).create({
            "name": "Historical Service",
            "qbo_id": "QBO-ITEM",
            "property_account_income_id": self.income.id,
        })

    def _engine(self):
        from ..services.qbo_sync_engine import QBOSyncEngine
        return QBOSyncEngine(self.env, self.mapping)

    def test_invoice_import_preserves_partner_product_and_amount(self):
        self._engine()._upsert_invoices([{
            "Id": "QBO-INVOICE-1",
            "SyncToken": "0",
            "DocNumber": "INV-100",
            "TxnDate": "2026-07-01",
            "CustomerRef": {"value": "QBO-CUSTOMER", "name": "Historical Customer"},
            "Line": [{
                "Amount": 250.0,
                "Description": "Consulting",
                "DetailType": "SalesItemLineDetail",
                "SalesItemLineDetail": {
                    "ItemRef": {"value": "QBO-ITEM", "name": "Historical Service"},
                    "Qty": 2,
                },
            }],
            "_qbo_type": "invoice",
        }])

        move = self.env["account.move"].search([("qbo_id", "=", "QBO-INVOICE-1")])
        self.assertEqual(move.partner_id, self.customer)
        self.assertEqual(move.ref, "INV-100")
        self.assertEqual(move.invoice_line_ids.product_id, self.product.product_variant_id)
        self.assertEqual(move.invoice_line_ids.price_subtotal, 250.0)

    def test_invoice_import_skips_subtotal_rollup_line(self):
        self._engine()._upsert_invoices([{
            "Id": "QBO-INVOICE-2",
            "SyncToken": "0",
            "DocNumber": "INV-200",
            "TxnDate": "2026-07-01",
            "CustomerRef": {"value": "QBO-CUSTOMER", "name": "Historical Customer"},
            "Line": [
                {
                    "Amount": 100.0,
                    "Description": "Consulting",
                    "DetailType": "SalesItemLineDetail",
                    "SalesItemLineDetail": {
                        "ItemRef": {"value": "QBO-ITEM", "name": "Historical Service"},
                        "Qty": 1,
                    },
                },
                {
                    "Amount": 200.0,
                    "Description": "Setup",
                    "DetailType": "SalesItemLineDetail",
                    "SalesItemLineDetail": {
                        "ItemRef": {"value": "QBO-ITEM", "name": "Historical Service"},
                        "Qty": 1,
                    },
                },
                {
                    "Amount": 300.0,
                    "DetailType": "SubTotalLineDetail",
                    "SubTotalLineDetail": {},
                },
            ],
            "_qbo_type": "invoice",
        }])

        move = self.env["account.move"].search([("qbo_id", "=", "QBO-INVOICE-2")])
        self.assertTrue(move)
        self.assertEqual(len(move.invoice_line_ids), 2)
        self.assertEqual(move.amount_total, 300.0)

    def test_bill_import_uses_qbo_account_and_replaces_lines_on_update(self):
        record = {
            "Id": "QBO-BILL-1",
            "SyncToken": "0",
            "DocNumber": "BILL-100",
            "TxnDate": "2026-07-02",
            "VendorRef": {"value": "QBO-VENDOR", "name": "Historical Vendor"},
            "Line": [{
                "Amount": 75.0,
                "Description": "Fuel",
                "DetailType": "AccountBasedExpenseLineDetail",
                "AccountBasedExpenseLineDetail": {
                    "AccountRef": {"value": "QBO-EXPENSE", "name": "Historical Expense"},
                },
            }],
            "_qbo_type": "bill",
        }
        engine = self._engine()
        engine._upsert_invoices([record])
        record["SyncToken"] = "1"
        record["Line"][0]["Amount"] = 80.0
        engine._upsert_invoices([record])

        move = self.env["account.move"].search([("qbo_id", "=", "QBO-BILL-1")])
        self.assertEqual(move.partner_id, self.vendor)
        self.assertEqual(len(move.invoice_line_ids), 1)
        self.assertEqual(move.invoice_line_ids.price_subtotal, 80.0)
        self.assertEqual(move.invoice_line_ids.account_id, self.expense)

    def test_payment_import_creates_customer_inbound_draft(self):
        engine = self._engine()
        engine._upsert_payments([{
            "Id": "QBO-PAY-1",
            "SyncToken": "0",
            "TxnDate": "2026-07-03",
            "TotalAmt": 100.0,
            "CustomerRef": {"value": "QBO-CUSTOMER", "name": "Historical Customer"},
            "DepositToAccountRef": {"value": "QBO-BANK", "name": "Historical Bank"},
        }], direction="pull")

        payment = self.env["account.payment"].search([("qbo_id", "=", "QBO-PAY-1")])
        self.assertTrue(payment)
        self.assertEqual(payment.partner_id, self.customer)
        self.assertEqual(payment.partner_type, "customer")
        self.assertEqual(payment.payment_type, "inbound")
        self.assertEqual(payment.amount, 100.0)
        self.assertEqual(payment.journal_id.code, "HBK")
        self.assertEqual(payment.state, "draft")

    def test_bill_payment_import_creates_supplier_outbound_draft(self):
        engine = self._engine()
        engine._upsert_payments([{
            "Id": "QBO-PAY-2",
            "SyncToken": "0",
            "TxnDate": "2026-07-04",
            "TotalAmt": 50.0,
            "VendorRef": {"value": "QBO-VENDOR", "name": "Historical Vendor"},
            "BankAccountRef": {"value": "QBO-BANK", "name": "Historical Bank"},
            "_qbo_type": "bill_payment",
        }], direction="pull")

        payment = self.env["account.payment"].search([("qbo_id", "=", "QBO-PAY-2")])
        self.assertTrue(payment)
        self.assertEqual(payment.partner_id, self.vendor)
        self.assertEqual(payment.partner_type, "supplier")
        self.assertEqual(payment.payment_type, "outbound")
        self.assertEqual(payment.amount, 50.0)
        self.assertEqual(payment.journal_id.code, "HBK")

    def test_live_journal_entry_import_creates_balanced_draft_move(self):
        engine = self._engine()
        engine._upsert_journal_entries([{
            "Id": "QBO-JE-1",
            "SyncToken": "0",
            "TxnDate": "2026-07-05",
            "DocNumber": "JE-1",
            "Line": [
                {
                    "Id": "1",
                    "Description": "Expense",
                    "Amount": 100.0,
                    "DetailType": "JournalEntryLineDetail",
                    "JournalEntryLineDetail": {
                        "PostingType": "Debit",
                        "AccountRef": {"name": "Historical Expense"},
                    },
                },
                {
                    "Id": "2",
                    "Description": "Revenue",
                    "Amount": -100.0,
                    "DetailType": "JournalEntryLineDetail",
                    "JournalEntryLineDetail": {
                        "PostingType": "Credit",
                        "AccountRef": {"name": "Historical Revenue"},
                    },
                },
            ],
        }], direction="pull")

        move = self.env["account.move"].search([("qbo_id", "=", "QBO-JE-1")])
        self.assertTrue(move)
        self.assertEqual(move.move_type, "entry")
        self.assertEqual(move.ref, "JE-1")
        self.assertEqual(move.journal_id.code, "HGN")
        self.assertEqual(move.state, "draft")
        self.assertEqual(sum(move.line_ids.mapped("debit")), 100.0)
        self.assertEqual(sum(move.line_ids.mapped("credit")), 100.0)

    def test_live_journal_entry_update_replaces_lines_on_draft(self):
        engine = self._engine()
        record = {
            "Id": "QBO-JE-2",
            "SyncToken": "0",
            "TxnDate": "2026-07-06",
            "DocNumber": "JE-2",
            "Line": [
                {
                    "Description": "Expense",
                    "Amount": 100.0,
                    "DetailType": "JournalEntryLineDetail",
                    "JournalEntryLineDetail": {
                        "PostingType": "Debit",
                        "AccountRef": {"name": "Historical Expense"},
                    },
                },
                {
                    "Description": "Revenue",
                    "Amount": -100.0,
                    "DetailType": "JournalEntryLineDetail",
                    "JournalEntryLineDetail": {
                        "PostingType": "Credit",
                        "AccountRef": {"name": "Historical Revenue"},
                    },
                },
            ],
        }
        engine._upsert_journal_entries([record], direction="pull")
        record["SyncToken"] = "1"
        record["Line"][0]["Amount"] = 125.0
        record["Line"][1]["Amount"] = -125.0
        engine._upsert_journal_entries([record], direction="pull")

        move = self.env["account.move"].search([("qbo_id", "=", "QBO-JE-2")])
        self.assertEqual(len(move.line_ids), 2)
        self.assertEqual(sum(move.line_ids.mapped("debit")), 125.0)
        self.assertEqual(move.qbo_sync_token, "1")


class TestQBOPullOnlyMVP(TransactionCase):
    """Phase 2: the MVP is hard pull-only and every log entry records an operation."""

    def setUp(self):
        super().setUp()
        self.company = self.env["res.company"].create({"name": "Pull Only Co"})
        self.realm = self.env["qbo.realm"].create({
            "name": "Pull Only Realm",
            "realm_id": "555000111",
            "client_id": "cid",
            "client_secret": "csec",
            "state": "connected",
            "sync_mode": "pull_only",
            "refresh_token": "rt",
        })
        self.mapping = self.env["qbo.company.mapping"].create({
            "company_id": self.company.id,
            "realm_id": self.realm.id,
            "sync_accounts": True,
            "sync_partners": False,
            "sync_invoices": False,
            "sync_payments": False,
            "sync_journal_entries": False,
            "sync_products": False,
        })
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

    def _make_engine(self):
        from ..services.qbo_sync_engine import QBOSyncEngine
        engine = QBOSyncEngine(self.env, self.mapping)
        engine.client = MagicMock()
        return engine

    def _logs(self, qbo_id):
        return self.env["qbo.sync.log"].search([
            ("qbo_id", "=", qbo_id),
            ("mapping_id", "=", self.mapping.id),
        ])

    # ── Pull-only: no QBO writes ──────────────────────────────────────────────

    def test_sync_all_pull_path_does_not_write_to_qbo(self):
        engine = self._make_engine()
        engine.client.get_accounts.return_value = [
            {
                "Id": "QBO-PULL-001",
                "SyncToken": "0",
                "Name": "Pulled Cash",
                "AccountType": "Bank",
                "Active": True,
                "MetaData": {"LastUpdatedTime": "2024-01-01T00:00:00-00:00"},
            },
        ]

        engine.sync_all()

        # The account was pulled into Odoo...
        acc = self.env["account.account"].with_company(self.company).search([
            ("qbo_id", "=", "QBO-PULL-001"),
            ("company_ids", "=", self.company.id),
        ])
        self.assertTrue(acc, "sync_all must still pull QBO → Odoo")
        # ...but nothing was written back to QuickBooks.
        engine.client.create_account.assert_not_called()
        engine.client.update_account.assert_not_called()

    def test_backfill_ignores_last_sync_and_clears_flag(self):
        import datetime

        engine = self._make_engine()
        engine.client.get_configuration.return_value = {
            "company_info": {"LegalName": "Pull Only Co"},
            "preferences": {},
            "bank_accounts": [],
        }
        engine.client.get_accounts.return_value = []
        self.mapping.write({
            "last_sync_accounts": datetime.datetime(2024, 1, 1),
            "historical_backfill": True,
        })

        engine.sync_all()

        engine.client.get_accounts.assert_called_once_with(modified_since=False)
        self.assertFalse(self.mapping.historical_backfill)

    def test_cron_pull_path_does_not_write_to_qbo(self):
        from ..services import qbo_sync_engine as engine_mod

        self.mapping.sync_requested = True
        with patch.object(engine_mod, "QBOApiClient") as MockClient:
            client = MockClient.return_value
            client.get_accounts.return_value = [
                {
                    "Id": "QBO-CRON-001",
                    "SyncToken": "0",
                    "Name": "Cron Pulled",
                    "AccountType": "Bank",
                    "Active": True,
                    "MetaData": {"LastUpdatedTime": "2024-01-01T00:00:00-00:00"},
                },
            ]
            self.env["qbo.realm"].cron_sync_all_realms()

            client.create_account.assert_not_called()
            client.update_account.assert_not_called()

        self.assertTrue(
            self.env["account.account"].with_company(self.company).search([
                ("qbo_id", "=", "QBO-CRON-001"),
                ("company_ids", "=", self.company.id),
            ]),
            "Cron pull path must still import the account",
        )

    # ── Operation recorded on every log entry ─────────────────────────────────

    def test_pull_create_log_has_create_operation(self):
        engine = self._make_engine()
        engine._upsert_accounts([
            {
                "Id": "QBO-OP-CREATE",
                "SyncToken": "0",
                "Name": "Fresh Account",
                "AccountType": "Bank",
                "Active": True,
                "MetaData": {"LastUpdatedTime": "2024-01-01T00:00:00-00:00"},
            },
        ], direction="pull")

        log = self._logs("QBO-OP-CREATE")
        self.assertTrue(log)
        self.assertEqual(log.status, "success")
        self.assertEqual(log.operation, "create")

    def test_pull_update_log_has_update_operation(self):
        self.env["account.account"].with_company(self.company).create({
            "name": "Pre-existing",
            "code": "4242",
            "account_type": "asset_cash",
            "qbo_id": "QBO-OP-UPDATE",
            "company_ids": [(4, self.company.id)],
        })
        engine = self._make_engine()
        engine._upsert_accounts([
            {
                "Id": "QBO-OP-UPDATE",
                "SyncToken": "1",
                "Name": "Renamed",
                "AccountType": "Bank",
                "Active": True,
                "MetaData": {"LastUpdatedTime": "2024-01-01T00:00:00-00:00"},
            },
        ], direction="pull")

        log = self._logs("QBO-OP-UPDATE")
        self.assertTrue(log)
        self.assertEqual(log.operation, "update")

    def test_conflict_log_has_conflict_operation(self):
        import datetime

        last_sync = datetime.datetime(2024, 1, 1, 0, 0, 0)
        self.mapping.write({"last_sync_accounts": last_sync})
        existing = self.env["account.account"].with_company(self.company).create({
            "name": "Disputed",
            "code": "4243",
            "account_type": "asset_cash",
            "qbo_id": "QBO-OP-CONFLICT",
            "company_ids": [(4, self.company.id)],
        })
        self.env.cr.execute(
            "UPDATE account_account SET write_date = %s WHERE id = %s",
            (datetime.datetime(2024, 6, 1), existing.id),
        )
        engine = self._make_engine()
        engine._upsert_accounts([
            {
                "Id": "QBO-OP-CONFLICT",
                "SyncToken": "2",
                "Name": "QBO Changed",
                "AccountType": "Bank",
                "Active": True,
                "MetaData": {"LastUpdatedTime": "2024-06-02T00:00:00-00:00"},
            },
        ], direction="pull")

        log = self._logs("QBO-OP-CONFLICT")
        self.assertTrue(log)
        self.assertEqual(log.status, "conflict")
        self.assertEqual(log.operation, "conflict")

    def test_skipped_log_has_skip_operation(self):
        engine = self._make_engine()
        engine._upsert_payments([
            {"Id": "QBO-OP-SKIP", "MetaData": {"LastUpdatedTime": "2024-01-01T00:00:00-00:00"}},
        ], direction="pull")

        log = self._logs("QBO-OP-SKIP")
        self.assertTrue(log)
        self.assertEqual(log.status, "skipped")
        self.assertEqual(log.operation, "skip")

    def test_error_log_has_error_operation(self):
        engine = self._make_engine()
        with patch.object(engine, "_map_account_to_odoo", side_effect=ValueError("boom")):
            engine._upsert_accounts([
                {
                    "Id": "QBO-OP-ERROR",
                    "SyncToken": "0",
                    "Name": "Will Fail",
                    "AccountType": "Bank",
                    "Active": True,
                    "MetaData": {"LastUpdatedTime": "2024-01-01T00:00:00-00:00"},
                },
            ], direction="pull")

        log = self._logs("QBO-OP-ERROR")
        self.assertTrue(log)
        self.assertEqual(log.status, "error")
        self.assertEqual(log.operation, "error")

    def test_missing_local_prerequisite_logs_skip_not_error(self):
        engine = self._make_engine()
        engine._upsert_invoices([
            {
                "Id": "QBO-MISSING-JOURNAL",
                "SyncToken": "0",
                "DocNumber": "INV-MISSING-JOURNAL",
                "TxnDate": "2026-01-01",
                "_qbo_type": "invoice",
            },
        ], direction="pull")

        log = self._logs("QBO-MISSING-JOURNAL")
        self.assertTrue(log)
        self.assertEqual(log.status, "skipped")
        self.assertEqual(log.operation, "skip")
        self.assertIn("No journal", log.message)

    def test_request_pull_queues_and_logs_queue_operation(self):
        from ..services.qbo_sync_engine import QBOSyncEngine

        with patch.object(QBOSyncEngine, "sync_all") as mock_sync:
            result = self.mapping.action_request_pull()

        mock_sync.assert_not_called()
        self.assertTrue(self.mapping.sync_requested)
        self.assertTrue(result.get("queued"))
        self.assertEqual(result.get("mode"), "pull")

        queue_logs = self.env["qbo.sync.log"].search([
            ("mapping_id", "=", self.mapping.id),
            ("operation", "=", "queue"),
        ])
        # Only accounts is enabled on this mapping.
        self.assertEqual(len(queue_logs), 1)
        self.assertEqual(queue_logs.entity_type, "account")
        self.assertEqual(queue_logs.direction, "pull")


class TestQBOManualSyncIsAsync(TransactionCase):
    """The manual trigger must enqueue, not run sync_all() inline.

    A full sync_all() on a large ledger overruns the BFF/RPC budget, so the
    manual path flags the mapping and lets the sync cron run it in a worker.
    """

    def setUp(self):
        super().setUp()
        self.company = self.env["res.company"].create({"name": "Async Sync Co"})
        self.realm = self.env["qbo.realm"].create({
            "name": "Async Realm",
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
            "sync_accounts": False,
            "sync_partners": False,
            "sync_invoices": False,
            "sync_payments": False,
            "sync_journal_entries": False,
            "sync_products": False,
        })
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

    def test_request_sync_flags_and_does_not_run_inline(self):
        from ..services.qbo_sync_engine import QBOSyncEngine

        with patch.object(QBOSyncEngine, "sync_all") as mock_sync:
            result = self.mapping.action_request_sync()

        mock_sync.assert_not_called()
        self.assertTrue(self.mapping.sync_requested)
        self.assertTrue(result.get("queued"))
        self.assertEqual(result.get("mapping_id"), self.mapping.id)

    def test_request_historical_backfill_flags_full_history_and_queues(self):
        from ..services.qbo_sync_engine import QBOSyncEngine

        with patch.object(QBOSyncEngine, "sync_all") as mock_sync:
            result = self.mapping.action_request_historical_backfill()

        mock_sync.assert_not_called()
        self.assertTrue(self.mapping.sync_requested)
        self.assertTrue(self.mapping.historical_backfill)
        self.assertTrue(result.get("queued"))

    def test_request_sync_rejects_upload_realm(self):
        self.realm.write({"sync_mode": "upload", "refresh_token": False})

        with self.assertRaisesRegex(Exception, "Connect or re-authorise QuickBooks"):
            self.mapping.action_request_sync()

        self.assertFalse(self.mapping.sync_requested)

    def test_cron_runs_requested_then_clears_flag(self):
        from ..services.qbo_sync_engine import QBOSyncEngine

        self.mapping.sync_requested = True
        with patch.object(QBOSyncEngine, "sync_all") as mock_sync:
            self.env["qbo.realm"].cron_sync_all_realms()

        mock_sync.assert_called_once()
        self.assertFalse(self.mapping.sync_requested, "flag must clear after the run")

    def test_cron_skips_upload_realm(self):
        from ..services.qbo_sync_engine import QBOSyncEngine

        self.realm.write({"sync_mode": "upload", "refresh_token": False})
        self.mapping.sync_requested = True
        with patch.object(QBOSyncEngine, "sync_all") as mock_sync:
            self.env["qbo.realm"].cron_sync_all_realms()

        mock_sync.assert_not_called()

    def test_cron_respects_interval_unless_requested(self):
        from ..services.qbo_sync_engine import QBOSyncEngine

        # Recently synced and not requested → cron skips it.
        self.mapping.write({
            "sync_interval_minutes": 60,
            "last_sync_date": fields.Datetime.now(),
        })
        with patch.object(QBOSyncEngine, "sync_all") as mock_skip:
            self.env["qbo.realm"].cron_sync_all_realms()
        mock_skip.assert_not_called()

        # A manual request overrides the interval throttle.
        self.mapping.sync_requested = True
        with patch.object(QBOSyncEngine, "sync_all") as mock_run:
            self.env["qbo.realm"].cron_sync_all_realms()
        mock_run.assert_called_once()
        self.assertFalse(self.mapping.sync_requested)
