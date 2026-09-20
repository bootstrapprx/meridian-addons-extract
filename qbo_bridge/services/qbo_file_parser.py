"""QBOFileParser — converts QBO file exports into the same canonical dict
structure that QBOApiClient returns from the live API.

Supported formats
-----------------
* CSV  — QBO report exports (column headers vary by report type)
* XLSX — same reports in Excel format
* JSON — manual API export (already close to API shape, minor normalization)

The goal is to produce dicts that QBOSyncEngine can consume identically
whether data came from the live API or a file upload.
"""
import csv
import io
import json
import logging
import zipfile
from pathlib import PurePosixPath

_logger = logging.getLogger(__name__)

# ── Entity type → expected column alias maps (QBO report headers vary) ─────
# Keys are lowercase normalized column names; values are canonical field names.
_ACCOUNT_COLUMNS = {
    "name": "Name",
    "account name": "Name",
    "type": "AccountType",
    "account type": "AccountType",
    "detail type": "AccountSubType",
    "subtype": "AccountSubType",
    "balance": "CurrentBalance",
    "current balance": "CurrentBalance",
    "description": "Description",
    "active": "Active",
}

_CUSTOMER_COLUMNS = {
    "customer": "DisplayName",
    "name": "DisplayName",
    "display name": "DisplayName",
    "company": "CompanyName",
    "email": "PrimaryEmailAddr",
    "phone": "PrimaryPhone",
    "balance": "Balance",
    "active": "Active",
}

_VENDOR_COLUMNS = {
    "vendor": "DisplayName",
    "name": "DisplayName",
    "display name": "DisplayName",
    "company": "CompanyName",
    "email": "PrimaryEmailAddr",
    "phone": "PrimaryPhone",
    "balance": "Balance",
    "active": "Active",
}

_INVOICE_COLUMNS = {
    "invoice no": "DocNumber",
    "invoice #": "DocNumber",
    "num": "DocNumber",
    "customer": "CustomerRef",
    "date": "TxnDate",
    "due date": "DueDate",
    "amount": "TotalAmt",
    "total": "TotalAmt",
    "balance": "Balance",
    "status": "EmailStatus",
}

_PRODUCT_COLUMNS = {
    "product/service": "Name",
    "name": "Name",
    "type": "Type",
    "description": "Description",
    "price": "UnitPrice",
    "sales price": "UnitPrice",
    "cost": "PurchaseCost",
    "sku": "Sku",
    "active": "Active",
}

_EMPLOYEE_COLUMNS = {
    "name": "Name",
    "employee": "Name",
    "display name": "Name",
    "first name": "GivenName",
    "given name": "GivenName",
    "last name": "FamilyName",
    "family name": "FamilyName",
    "email": "PrimaryEmailAddr",
    "email address": "PrimaryEmailAddr",
    "phone": "PrimaryPhone",
    "mobile": "Mobile",
    "title": "Title",
    "active": "Active",
}

_COLUMN_MAPS = {
    "account": _ACCOUNT_COLUMNS,
    "partner": {**_CUSTOMER_COLUMNS, **_VENDOR_COLUMNS},
    "customer": _CUSTOMER_COLUMNS,
    "vendor": _VENDOR_COLUMNS,
    "invoice": _INVOICE_COLUMNS,
    "product": _PRODUCT_COLUMNS,
    "employee": _EMPLOYEE_COLUMNS,
}

_QBO_PACKAGE_FILES = {
    "balance_sheet.xlsx": {"dataset": "balance_sheet", "importable": False, "kind": "report"},
    "customers.xlsx": {"dataset": "customers", "importable": True, "kind": "contact"},
    "employees.xlsx": {"dataset": "employees", "importable": True, "kind": "contact"},
    "general_ledger.xlsx": {"dataset": "general_ledger", "importable": True, "kind": "ledger"},
    "journal.xlsx": {"dataset": "journal", "importable": True, "kind": "journal"},
    "profit_and_loss.xlsx": {"dataset": "profit_and_loss", "importable": False, "kind": "report"},
    "trial_balance.xlsx": {"dataset": "trial_balance", "importable": False, "kind": "report"},
    "vendors.xlsx": {"dataset": "vendors", "importable": True, "kind": "contact"},
}


class QBOFileParseError(Exception):
    pass


class QBOFileParser:
    """Parse a QBO file export and return a list of normalized entity dicts."""

    def parse(self, file_content: bytes, file_type: str, entity_type: str) -> list[dict]:
        """Entry point.

        Parameters
        ----------
        file_content : bytes
            Raw file bytes.
        file_type : str
            One of ``csv``, ``xlsx``, ``json``.
        entity_type : str
            One of ``account``, ``partner``, ``invoice``, ``payment``,
            ``journal_entry``, ``product``.

        Returns
        -------
        list[dict]
            List of normalized dicts ready for QBOSyncEngine consumption.
        """
        ftype = file_type.lower().strip(".")
        if ftype == "csv":
            rows = self._parse_csv(file_content)
        elif ftype in ("xlsx", "xls"):
            rows = self._parse_xlsx(file_content)
        elif ftype == "json":
            return self._parse_json(file_content, entity_type)
        else:
            raise QBOFileParseError(f"Unsupported file type: {file_type}")

        return [self._normalize_row(row, entity_type) for row in rows if row]

    def parse_package(self, file_content: bytes) -> dict:
        """Parse a QuickBooks export zip package.

        The package is expected to contain the standard QuickBooks export
        workbook names exactly as provided by the user flow.
        """
        try:
            archive = zipfile.ZipFile(io.BytesIO(file_content))
        except zipfile.BadZipFile as exc:
            raise QBOFileParseError("Invalid ZIP package.") from exc

        discovered = {}
        unexpected_files = []
        for member in archive.infolist():
            if member.is_dir():
                continue
            safe_name = self._safe_zip_member_name(member.filename)
            base_name = safe_name.casefold()
            if base_name not in _QBO_PACKAGE_FILES:
                unexpected_files.append(member.filename)
                continue
            if base_name in discovered:
                raise QBOFileParseError(f"Duplicate workbook in package: {member.filename}")
            discovered[base_name] = member

        missing_files = [name for name in _QBO_PACKAGE_FILES if name not in discovered]
        if missing_files:
            raise QBOFileParseError(
                "Missing required QuickBooks export files: %s" % ", ".join(sorted(missing_files)),
            )

        package = {
            "metadata": {
                "file_count": len([info for info in archive.infolist() if not info.is_dir()]),
                "unexpected_files": unexpected_files,
                "required_files": list(_QBO_PACKAGE_FILES),
            },
            "customers": [],
            "vendors": [],
            "employees": [],
            "journal_entries": [],
            "reports": {},
            "files": {},
        }

        for file_name, file_spec in _QBO_PACKAGE_FILES.items():
            member = discovered[file_name]
            raw = archive.read(member)
            rows = self._parse_xlsx(raw)
            if not rows:
                raise QBOFileParseError(f"Workbook {member.filename} does not contain any data rows.")

            headers = list(rows[0].keys()) if rows else []
            package["files"][file_spec["dataset"]] = {
                "filename": member.filename,
                "row_count": len(rows),
                "headers": headers,
                "kind": file_spec["kind"],
            }

            if file_spec["dataset"] == "customers":
                package["customers"] = [
                    self._normalize_row(row, "partner") for row in rows if row
                ]
                for row in package["customers"]:
                    row["_qbo_type"] = "customer"
                    row["_source_file"] = member.filename
            elif file_spec["dataset"] == "vendors":
                package["vendors"] = [
                    self._normalize_row(row, "partner") for row in rows if row
                ]
                for row in package["vendors"]:
                    row["_qbo_type"] = "vendor"
                    row["_source_file"] = member.filename
            elif file_spec["dataset"] == "employees":
                package["employees"] = [
                    self._normalize_row(row, "employee") for row in rows if row
                ]
                for row in package["employees"]:
                    row["_source_file"] = member.filename
            elif file_spec["dataset"] in {"journal", "general_ledger"}:
                normalized_rows = [self._normalize_report_row(row, member.filename) for row in rows if row]
                package["journal_entries"].extend(normalized_rows)
            else:
                package["reports"][file_spec["dataset"]] = [
                    self._normalize_report_row(row, member.filename) for row in rows if row
                ]

        return package

    # ── Raw readers ───────────────────────────────────────────────────────────

    def _parse_csv(self, content: bytes) -> list[dict]:
        """Read CSV bytes into a list of dicts (headers as keys)."""
        text = content.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        return [dict(row) for row in reader]

    def _parse_xlsx(self, content: bytes) -> list[dict]:
        """Read XLSX bytes using openpyxl into a list of dicts."""
        try:
            import openpyxl
        except ImportError as exc:
            raise QBOFileParseError(
                "openpyxl is required for XLSX import. Install it with: pip install openpyxl",
            ) from exc

        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []

        # First non-empty row is the header
        header_idx = 0
        for i, row in enumerate(rows):
            if any(cell for cell in row):
                header_idx = i
                break

        headers = [str(c).strip() if c is not None else "" for c in rows[header_idx]]
        result = []
        for row in rows[header_idx + 1:]:
            if not any(cell for cell in row):
                continue
            result.append(
                {headers[j]: (str(cell).strip() if cell is not None else "") for j, cell in enumerate(row)},
            )
        return result

    def _parse_json(self, content: bytes, entity_type: str) -> list[dict]:
        """Parse JSON export. QBO JSON exports are already close to API shape."""
        try:
            data = json.loads(content.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise QBOFileParseError(f"Invalid JSON: {exc}") from exc

        # Handle both a raw list and the QueryResponse wrapper shape
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # QueryResponse wrapper
            qr = data.get("QueryResponse", {})
            for key in ("Account", "Customer", "Vendor", "Invoice", "Bill",
                        "Payment", "JournalEntry", "Item"):
                if key in qr:
                    return qr[key]
            # Flat dict with entity array at top level
            for key in data:
                if isinstance(data[key], list):
                    return data[key]
        return []

    # ── Normalizer ────────────────────────────────────────────────────────────

    def _normalize_row(self, row: dict, entity_type: str) -> dict:
        """Map arbitrary CSV/XLSX column headers to canonical QBO API field names."""
        col_map = _COLUMN_MAPS.get(entity_type, {})
        normalized = {}
        for raw_key, value in row.items():
            canonical = col_map.get(raw_key.strip().lower())
            if canonical:
                normalized[canonical] = value
            else:
                # Preserve unknown columns under their original name
                normalized[raw_key] = value

        # Coerce Active field to boolean
        if "Active" in normalized:
            normalized["Active"] = str(normalized["Active"]).strip().lower() not in (
                "false", "0", "no", "inactive", "",
            )

        # Mark as file-sourced so the sync engine skips the push-back check
        normalized["_source"] = "file"
        return normalized

    def _normalize_report_row(self, row: dict, source_file: str) -> dict:
        normalized = {}
        for raw_key, value in row.items():
            key = str(raw_key).strip()
            if key:
                normalized[key] = value
        normalized["_source"] = "file"
        normalized["_source_file"] = source_file
        return normalized

    def _safe_zip_member_name(self, member_name: str) -> str:
        if not member_name:
            raise QBOFileParseError("ZIP archive contains an empty entry name.")
        if "\\" in member_name:
            raise QBOFileParseError(f"Unsafe ZIP path detected: {member_name}")
        path = PurePosixPath(member_name)
        if any(part == ".." for part in path.parts):
            raise QBOFileParseError(f"Unsafe ZIP path detected: {member_name}")
        return path.name
