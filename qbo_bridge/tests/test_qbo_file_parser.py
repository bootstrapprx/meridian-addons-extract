"""Unit tests for QBOFileParser.

These tests exercise the parser in isolation — no Odoo database required.
Run with:
    python -m pytest custom_addons/qbo_bridge/tests/test_qbo_file_parser.py -v
Or via Odoo test runner:
    ./odoo-bin -c ... -d ktest --test-enable -i qbo_bridge --stop-after-init
"""
import json
import zipfile
import unittest
from io import BytesIO

from ..services.qbo_file_parser import QBOFileParseError, QBOFileParser


class TestQBOFileParserCSV(unittest.TestCase):

    def setUp(self):
        self.parser = QBOFileParser()

    def _csv(self, headers, rows):
        lines = [",".join(headers)]
        for row in rows:
            lines.append(",".join(str(v) for v in row))
        return "\n".join(lines).encode("utf-8")

    # ── Account CSV ───────────────────────────────────────────────────────────

    def test_account_csv_basic(self):
        content = self._csv(
            ["Name", "Account Type", "Detail Type", "Balance", "Active"],
            [["Cash", "Bank", "Checking", "1000.00", "true"]],
        )
        records = self.parser.parse(content, "csv", "account")
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r["Name"], "Cash")
        self.assertEqual(r["AccountType"], "Bank")
        self.assertTrue(r["Active"])
        self.assertEqual(r["_source"], "file")

    def test_account_csv_inactive(self):
        content = self._csv(
            ["Name", "Account Type", "Active"],
            [["Old Account", "Expense", "false"]],
        )
        records = self.parser.parse(content, "csv", "account")
        self.assertFalse(records[0]["Active"])

    def test_account_csv_unknown_columns_preserved(self):
        content = self._csv(
            ["Name", "Account Type", "CustomField"],
            [["Revenue", "Income", "some value"]],
        )
        records = self.parser.parse(content, "csv", "account")
        self.assertIn("CustomField", records[0])

    def test_empty_csv_returns_empty_list(self):
        content = b"Name,Account Type\n"
        records = self.parser.parse(content, "csv", "account")
        self.assertEqual(records, [])

    # ── Partner CSV ───────────────────────────────────────────────────────────

    def test_customer_csv(self):
        content = self._csv(
            ["Customer", "Company", "Email", "Active"],
            [["John Doe", "Doe Corp", "john@doe.com", "True"]],
        )
        records = self.parser.parse(content, "csv", "partner")
        self.assertEqual(records[0]["DisplayName"], "John Doe")
        self.assertEqual(records[0]["PrimaryEmailAddr"], "john@doe.com")

    # ── Product CSV ───────────────────────────────────────────────────────────

    def test_product_csv(self):
        content = self._csv(
            ["Product/Service", "Type", "Sales Price", "Cost"],
            [["Widget A", "Inventory", "49.99", "20.00"]],
        )
        records = self.parser.parse(content, "csv", "product")
        self.assertEqual(records[0]["Name"], "Widget A")
        self.assertEqual(records[0]["UnitPrice"], "49.99")

    # ── Unsupported format ────────────────────────────────────────────────────

    def test_unsupported_format_raises(self):
        with self.assertRaises(QBOFileParseError):
            self.parser.parse(b"data", "pdf", "account")


class TestQBOFileParserJSON(unittest.TestCase):

    def setUp(self):
        self.parser = QBOFileParser()

    def test_json_list(self):
        data = [{"Id": "1", "Name": "Sales", "AccountType": "Income"}]
        records = self.parser.parse(json.dumps(data).encode(), "json", "account")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["Name"], "Sales")

    def test_json_query_response_wrapper(self):
        data = {"QueryResponse": {"Account": [{"Id": "2", "Name": "Rent", "AccountType": "Expense"}]}}
        records = self.parser.parse(json.dumps(data).encode(), "json", "account")
        self.assertEqual(records[0]["Name"], "Rent")

    def test_json_invalid_raises(self):
        with self.assertRaises(QBOFileParseError):
            self.parser.parse(b"not json at all {{{", "json", "account")

    def test_json_empty_list(self):
        records = self.parser.parse(b"[]", "json", "account")
        self.assertEqual(records, [])


class TestQBOFileParserXLSX(unittest.TestCase):
    """XLSX tests use openpyxl to build a real workbook in memory."""

    def setUp(self):
        self.parser = QBOFileParser()

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

    def test_account_xlsx(self):
        content = self._build_xlsx(
            ["Name", "Account Type", "Balance"],
            [["Payroll", "Expense", "5000"]],
        )
        records = self.parser.parse(content, "xlsx", "account")
        self.assertEqual(records[0]["Name"], "Payroll")
        self.assertEqual(records[0]["AccountType"], "Expense")

    def test_xlsx_skips_empty_rows(self):
        content = self._build_xlsx(
            ["Name", "Account Type"],
            [["Valid", "Bank"], [None, None], ["", ""]],
        )
        records = self.parser.parse(content, "xlsx", "account")
        # Only the first non-empty data row should survive
        self.assertEqual(len(records), 1)


class TestQBOFileParserPackage(unittest.TestCase):
    def setUp(self):
        self.parser = QBOFileParser()

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

    def _build_package(self, files):
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for filename, content in files.items():
                archive.writestr(filename, content)
        return buf.getvalue()

    def test_package_parse_accepts_expected_quickbooks_bundle(self):
        package = self._build_package(
            {
                "Balance_sheet.xlsx": self._build_xlsx(["Account", "Amount"], [["Cash", "100.00"]]),
                "Customers.xlsx": self._build_xlsx(["Customer", "Email", "Active"], [["Jane Doe", "jane@example.com", "true"]]),
                "Employees.xlsx": self._build_xlsx(["Name", "Email", "Active"], [["John Staff", "john@example.com", "true"]]),
                "General_ledger.xlsx": self._build_xlsx(
                    ["TxnDate", "DocNumber", "Account", "Debit", "Credit", "Memo"],
                    [["2026-05-11", "GL-1", "Cash", "100.00", "0.00", "Deposit"]],
                ),
                "Journal.xlsx": self._build_xlsx(
                    ["TxnDate", "DocNumber", "Account", "Debit", "Credit", "Memo"],
                    [["2026-05-11", "JRN-1", "Sales", "0.00", "100.00", "Revenue"]],
                ),
                "Profit_and_loss.xlsx": self._build_xlsx(["Account", "Amount"], [["Revenue", "100.00"]]),
                "Trial_balance.xlsx": self._build_xlsx(["Account", "Debit", "Credit"], [["Cash", "100.00", "0.00"]]),
                "Vendors.xlsx": self._build_xlsx(["Vendor", "Email", "Active"], [["Acme Corp", "ap@acme.example", "true"]]),
            },
        )

        parsed = self.parser.parse_package(package)

        self.assertEqual(len(parsed["customers"]), 1)
        self.assertEqual(len(parsed["vendors"]), 1)
        self.assertEqual(len(parsed["employees"]), 1)
        self.assertEqual(len(parsed["journal_entries"]), 2)
        self.assertIn("balance_sheet", parsed["reports"])
        self.assertIn("trial_balance", parsed["reports"])
        self.assertIn("profit_and_loss", parsed["reports"])
        self.assertEqual(parsed["files"]["customers"]["row_count"], 1)

    def test_package_parse_rejects_missing_required_files(self):
        package = self._build_package(
            {
                "Customers.xlsx": self._build_xlsx(["Customer"], [["Jane Doe"]]),
            },
        )

        with self.assertRaises(QBOFileParseError):
            self.parser.parse_package(package)


if __name__ == "__main__":
    unittest.main()
