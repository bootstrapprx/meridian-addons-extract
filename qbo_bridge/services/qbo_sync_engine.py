"""QBOSyncEngine — pulls data from QBO into Odoo (MVP is pull-only).

The MVP is hard pull-only: ``sync_all`` and the cron read from QuickBooks and
write into Odoo only. They never write back to QuickBooks. The push helpers
(Odoo → QBO) are kept as a dormant future surface and are deliberately not
invoked by the sync flow, the cron, the BFF pull action, or conflict
resolution.

Conflict strategy
-----------------
When both sides have changed since ``last_sync``:
  1. A ``qbo.conflict`` record is created with JSON snapshots of both versions.
  2. A ``qbo.sync.log`` record is written with ``status='conflict'``.
  3. The record is NOT written to either side until the user resolves it.

Entity field mappings (QBO → Odoo) are implemented as ``_map_*`` methods.
Push mappings (Odoo → QBO) are in the corresponding ``_odoo_to_qbo_*`` methods,
which remain dormant in the MVP.

TODO for each entity
--------------------
Each ``_sync_*`` method has a ``# TODO`` marking where the Odoo model write /
update logic needs to be completed with your specific chart of account
structure, journal types, and partner categories.
"""
import json
import logging
import re
import time
from contextlib import suppress
from collections import defaultdict
from datetime import datetime, timezone

from odoo import fields
from odoo.exceptions import UserError, ValidationError

from ..models.qbo_sync_log import QboSyncLog
from .qbo_api_client import QBOApiClient, QBOApiError
from .qbo_readiness import qbo_operational_account

_logger = logging.getLogger(__name__)

# Fields used to detect meaningful changes (by entity type)
_CONFLICT_FIELDS = {
    "account": ["Name", "AccountType", "AccountSubType", "Active", "Description"],
    "partner": ["DisplayName", "CompanyName", "PrimaryEmailAddr", "Active", "Balance"],
    "invoice": ["TotalAmt", "Balance", "DueDate", "EmailStatus"],
    "payment": ["TotalAmt", "TxnDate"],
    "journal_entry": ["TotalAmt", "TxnDate", "Adjustment"],
    "product": ["Name", "UnitPrice", "PurchaseCost", "Type", "Active"],
}

# Odoo model names per entity type
_ODOO_MODELS = {
    "account": "account.account",
    "partner": "res.partner",
    "employee": "hr.employee",
    "invoice": "account.move",
    "payment": "account.payment",
    "journal_entry": "account.move",
    "product": "product.template",
}

# qbo.sync.log operation → QBOSyncEngine._stats counter.
_OPERATION_STATS = {
    "create": "created",
    "update": "updated",
    "skip": "skipped",
    "conflict": "conflicts",
    "error": "errors",
}


class QBOSyncEngine:
    """Stateful sync session for one qbo.company.mapping record."""

    def __init__(self, env, mapping):
        self.env = env
        self.mapping = mapping
        self.client = QBOApiClient(mapping.realm_id)
        self._backfill = False
        self._stats = {
            "created": 0, "updated": 0, "skipped": 0, "conflicts": 0, "errors": 0,
        }

    # =========================================================================
    # Public entry points
    # =========================================================================

    def sync_all(self):
        """Run all enabled entity syncs for this mapping."""
        m = self.mapping
        _logger.info("QBO sync start: %s", m.display_name)
        self._backfill = m.historical_backfill
        try:
            self._sync_configuration()
            if m.sync_accounts:
                self._safe_sync("account", self._sync_accounts)
            if m.sync_partners:
                self._safe_sync("partner", self._sync_partners)
            if m.sync_products:
                self._safe_sync("product", self._sync_products)
            if m.sync_invoices:
                self._safe_sync("invoice", self._sync_invoices)
            if m.sync_payments:
                self._safe_sync("payment", self._sync_payments)
            if m.sync_journal_entries:
                self._safe_sync("journal_entry", self._sync_journal_entries)
        finally:
            self._backfill = False
            if m.historical_backfill:
                m.write({"historical_backfill": False})
        _logger.info("QBO sync complete: %s | %s", m.display_name, self._stats)

    def _sync_configuration(self):
        """Receive company settings without applying legal or fiscal decisions."""
        t0 = time.monotonic()
        try:
            snapshot = self.client.get_configuration()
            self.mapping.sudo().write(
                {
                    "configuration_snapshot": snapshot,
                    "configuration_synced_at": fields.Datetime.now(),
                },
            )
            self._log(
                "mapping",
                "pull",
                "success",
                "update",
                self.mapping.realm_id.realm_id,
                None,
                t0,
                "Received QBO company information and preferences for setup review.",
            )
        except Exception as exc:
            # Configuration enrichment must not prevent the accounting pull.
            _logger.warning("QBO configuration snapshot failed: %s", exc)
            self._log(
                "mapping",
                "pull",
                "error",
                "error",
                self.mapping.realm_id.realm_id,
                None,
                t0,
                str(exc),
            )

    def sync_from_file(self, records: list[dict], entity_type: str):
        """Import pre-parsed file records for one entity type."""
        dispatcher = {
            "account": self._upsert_accounts,
            "partner": self._upsert_partners,
            "employee": self._upsert_employees,
            "invoice": self._upsert_invoices,
            "payment": self._upsert_payments,
            "product": self._upsert_products,
            "journal_entry": self._upsert_journal_entries,
        }
        fn = dispatcher.get(entity_type)
        if not fn:
            raise ValueError(f"Unknown entity type: {entity_type}")
        fn(records, direction="pull")

    def sync_from_package(self, package):
        """Import a validated QuickBooks package in one pass.

        Returns a high-level summary that the wizard can present to the user.
        """
        summary = {
            "customers": 0,
            "vendors": 0,
            "employees": 0,
            "journal_entries": 0,
            "reports": {},
            "warnings": [],
        }

        if package.get("customers"):
            before = self._stats.copy()
            self._upsert_partners(package["customers"], direction="pull")
            summary["customers"] = self._stats["created"] + self._stats["updated"] - before["created"] - before["updated"]

        if package.get("vendors"):
            before = self._stats.copy()
            self._upsert_partners(package["vendors"], direction="pull")
            summary["vendors"] = self._stats["created"] + self._stats["updated"] - before["created"] - before["updated"]

        if package.get("employees"):
            before = self._stats.copy()
            self._upsert_employees(package["employees"], direction="pull")
            summary["employees"] = self._stats["created"] + self._stats["updated"] - before["created"] - before["updated"]
            if "hr.employee" not in self.env:
                summary["warnings"].append(
                    "Employees were validated but skipped because hr.employee is not installed in this database.",
                )

        if package.get("journal_entries"):
            before = self._stats.copy()
            self._upsert_journal_entries(package["journal_entries"], direction="pull")
            summary["journal_entries"] = self._stats["created"] + self._stats["updated"] - before["created"] - before["updated"]
            summary["warnings"].append(
                "Journal and ledger rows were validated; balanced move creation depends on matching accounts and a general journal.",
            )

        for report_name, rows in (package.get("reports") or {}).items():
            summary["reports"][report_name] = len(rows)
            summary["warnings"].append(
                f"{report_name.replace('_', ' ').title()} was validated ({len(rows)} rows) but is kept as a report artifact.",
            )

        return summary

    # =========================================================================
    # Per-entity sync orchestrators
    # =========================================================================

    def _sync_since(self, entity_type):
        """Timestamp filter for one entity; full history ignores it."""
        if self._backfill:
            return False
        return self.mapping.get_last_sync_for(entity_type)

    def _sync_accounts(self):
        # MVP is pull-only: QBO → Odoo. No write-back to QuickBooks here.
        qbo_records = self.client.get_accounts(modified_since=self._sync_since("account"))
        self._upsert_accounts(qbo_records, direction="pull")
        self.mapping.set_last_sync_for("account")

    def _sync_partners(self):
        # MVP is pull-only: QBO → Odoo. No write-back to QuickBooks here.
        customers = self.client.get_customers(modified_since=self._sync_since("partner"))
        vendors = self.client.get_vendors(modified_since=self._sync_since("partner"))
        all_partners = [{"_qbo_type": "customer", **r} for r in customers] + \
                       [{"_qbo_type": "vendor", **r} for r in vendors]
        self._upsert_partners(all_partners, direction="pull")
        self.mapping.set_last_sync_for("partner")

    def _sync_invoices(self):
        # MVP is pull-only: QBO → Odoo. No write-back to QuickBooks here.
        invoices = self.client.get_invoices(modified_since=self._sync_since("invoice"))
        bills = self.client.get_bills(modified_since=self._sync_since("invoice"))
        combined = [{"_qbo_type": "invoice", **r} for r in invoices] + \
                   [{"_qbo_type": "bill", **r} for r in bills]
        self._upsert_invoices(combined, direction="pull")
        self.mapping.set_last_sync_for("invoice")

    def _sync_payments(self):
        payments = self.client.get_payments(modified_since=self._sync_since("payment"))
        bill_payments = self.client.get_bill_payments(modified_since=self._sync_since("payment"))
        combined = [{"_qbo_type": "payment", **r} for r in payments] + \
                   [{"_qbo_type": "bill_payment", **r} for r in bill_payments]
        self._upsert_payments(combined, direction="pull")
        self.mapping.set_last_sync_for("payment")

    def _sync_journal_entries(self):
        entries = self.client.get_journal_entries(modified_since=self._sync_since("journal_entry"))
        self._upsert_journal_entries(entries, direction="pull")
        self.mapping.set_last_sync_for("journal_entry")

    def _sync_products(self):
        # MVP is pull-only: QBO → Odoo. No write-back to QuickBooks here.
        items = self.client.get_items(modified_since=self._sync_since("product"))
        self._upsert_products(items, direction="pull")
        self.mapping.set_last_sync_for("product")

    # =========================================================================
    # Upsert helpers (QBO → Odoo, pull direction)
    # =========================================================================

    def _upsert_accounts(self, qbo_records, direction="pull"):
        """Create or update Odoo account.account records from QBO Account data."""
        AccountAccount = self.env["account.account"].with_company(self.mapping.company_id)
        has_standard_chart = "qbo_standard_account_id" in AccountAccount._fields
        for rec in qbo_records:
            t0 = time.monotonic()
            qbo_id = rec.get("Id")
            try:
                odoo_vals = self._map_account_to_odoo(rec)
                existing = AccountAccount.search(
                    [
                        ("qbo_id", "=", qbo_id),
                        ("company_ids", "=", self.mapping.company_id.id),
                    ],
                    limit=1,
                ) if qbo_id else None
                if not existing and odoo_vals.get("code"):
                    canonical = has_standard_chart and AccountAccount.search(
                        [
                            ("company_ids", "=", self.mapping.company_id.id),
                            ("code", "=", odoo_vals["code"]),
                            ("qbo_standard_account_id", "!=", False),
                        ],
                        limit=1,
                    )
                    bridge_rule = self._match_account_bridge_rule(rec)
                    if not canonical and bridge_rule and bridge_rule.canonical_code == odoo_vals["code"]:
                        canonical = AccountAccount.search([
                            ("company_ids", "=", self.mapping.company_id.id),
                            ("code", "=", bridge_rule.canonical_code), ("qbo_id", "=", False),
                        ], limit=1)
                    if canonical:
                        odoo_vals["code"] = f"QBO.{self._normalize_account_code(None, qbo_id)}"[:64]
                    domain = [
                        ("company_ids", "=", self.mapping.company_id.id),
                        ("code", "=", odoo_vals["code"]),
                        ("qbo_id", "=", False),
                    ]
                    if has_standard_chart:
                        domain.append(("qbo_standard_account_id", "=", False))
                    existing = AccountAccount.search(domain, limit=1)

                if existing:
                    conflict = self._detect_conflict(existing, rec, "account")
                    if conflict:
                        self._create_conflict(existing, rec, "account")
                        continue
                    existing.write(odoo_vals)
                    self._log("account", direction, "success", "update", qbo_id, existing, t0)
                else:
                    new_rec = AccountAccount.create(odoo_vals)
                    self._log("account", direction, "success", "create", qbo_id, new_rec, t0)
            except Exception as exc:
                self._log_upsert_exception("account", direction, qbo_id, None, t0, exc)

    def _upsert_partners(self, qbo_records, direction="pull"):
        """Create or update res.partner records from QBO Customer/Vendor data."""
        Partner = self.env["res.partner"].with_company(self.mapping.company_id)
        for rec in qbo_records:
            t0 = time.monotonic()
            qbo_id = rec.get("Id")
            try:
                odoo_vals = self._map_partner_to_odoo(rec)
                existing = Partner.search([("qbo_id", "=", qbo_id)], limit=1) if qbo_id else None
                if existing:
                    if self._detect_conflict(existing, rec, "partner"):
                        self._create_conflict(existing, rec, "partner")
                        continue
                    existing.write(odoo_vals)
                    self._log("partner", direction, "success", "update", qbo_id, existing, t0)
                else:
                    new_rec = Partner.create(odoo_vals)
                    self._log("partner", direction, "success", "create", qbo_id, new_rec, t0)
            except Exception as exc:
                self._log_upsert_exception("partner", direction, qbo_id, None, t0, exc)

    def _upsert_invoices(self, qbo_records, direction="pull"):
        """Create or update account.move records from QBO Invoice/Bill data."""
        Move = self.env["account.move"].with_company(self.mapping.company_id)
        for rec in qbo_records:
            t0 = time.monotonic()
            qbo_id = rec.get("Id")
            try:
                with self.env.cr.savepoint():
                    odoo_vals = self._map_invoice_to_odoo(rec)
                    existing = self._find_source_record("account.move", rec.get("_qbo_type", "invoice"), qbo_id)
                    if existing:
                        if self._detect_conflict(existing, rec, "invoice"):
                            self._create_conflict(existing, rec, "invoice")
                            continue
                        if existing.state == "draft":
                            odoo_vals["invoice_line_ids"] = [
                                (5, 0, 0),
                                *odoo_vals["invoice_line_ids"],
                            ]
                            existing.write(odoo_vals)
                            existing._qbo_validate_conversion()
                            self._log("invoice", direction, "success", "update", qbo_id, existing, t0)
                        else:
                            # Posted invoices are immutable here — record an honest skip
                            # rather than implying we updated a posted move.
                            self._log(
                                "invoice", direction, "skipped", "skip", qbo_id, existing, t0,
                                "Posted invoice left unchanged (pull cannot modify a posted move)",
                            )
                    else:
                        new_rec = Move.create(odoo_vals)
                        new_rec._qbo_validate_conversion()
                        self._log("invoice", direction, "success", "create", qbo_id, new_rec, t0)
            except Exception as exc:
                self._log_upsert_exception("invoice", direction, qbo_id, None, t0, exc)

    def _upsert_payments(self, qbo_records, direction="pull"):
        """Create or update draft account.payment records from QBO payments."""
        Payment = self.env["account.payment"].with_company(self.mapping.company_id)
        for rec in qbo_records:
            t0 = time.monotonic()
            qbo_id = rec.get("Id")
            try:
                with self.env.cr.savepoint():
                    vals = self._map_payment_to_odoo(rec)
                    if not vals:
                        self._log(
                            "payment",
                            direction,
                            "skipped",
                            "skip",
                            qbo_id,
                            None,
                            t0,
                            "Missing local prerequisite (partner, journal, method or amount)",
                        )
                        continue
                    existing = self._find_source_record("account.payment", rec.get("_qbo_type", "payment"), qbo_id)
                    if existing:
                        if existing.state == "draft":
                            existing.write(vals)
                            self._log("payment", direction, "success", "update", qbo_id, existing, t0)
                        else:
                            self._log(
                                "payment",
                                direction,
                                "skipped",
                                "skip",
                                qbo_id,
                                existing,
                                t0,
                                "Posted payment left unchanged (pull cannot modify a posted payment)",
                            )
                    else:
                        new_rec = Payment.create(vals)
                        self._log("payment", direction, "success", "create", qbo_id, new_rec, t0)
            except Exception as exc:
                self._log_upsert_exception("payment", direction, qbo_id, None, t0, exc)

    def _map_payment_to_odoo(self, rec):
        """Map a QBO Payment/BillPayment to draft account.payment values."""
        if rec.get("_qbo_type", "payment") not in ("payment", "bill_payment"):
            raise UserError("This source payment type requires a supported translation.")
        if rec.get("_qbo_type") == "bill_payment":
            payment_type, partner_type = "outbound", "supplier"
            partner_ref = rec.get("VendorRef")
        else:
            payment_type, partner_type = "inbound", "customer"
            partner_ref = rec.get("CustomerRef")

        partner_id, partner_name = self._qbo_ref(partner_ref)
        Partner = self.env["res.partner"].with_company(self.mapping.company_id)
        partner = partner_id and Partner.search(
            [
                ("company_id", "in", [False, self.mapping.company_id.id]),
                ("qbo_id", "=", partner_id),
            ],
            limit=1,
        )
        if not partner:
            return None

        journal = self._payment_journal(rec)
        if not journal:
            return None
        method_line = journal._get_available_payment_method_lines(payment_type)[:1]
        if not method_line:
            return None

        amount = self._float_value(rec.get("TotalAmt") or rec.get("Amount"))
        date_value = rec.get("TxnDate") or rec.get("Date")
        if amount <= 0 or not date_value:
            return None

        return {
            **self._payment_reference_values(rec, journal),
            "company_id": self.mapping.company_id.id,
            "partner_id": partner.id,
            "partner_type": partner_type,
            "payment_type": payment_type,
            "journal_id": journal.id,
            "payment_method_line_id": method_line.id,
            "date": date_value,
            "amount": amount,
            "payment_reference": rec.get("PaymentRefNum") or rec.get("DocNumber") or "",
            "memo": rec.get("PrivateNote") or rec.get("Memo") or "",
            "qbo_id": rec.get("Id"),
            "qbo_sync_token": rec.get("SyncToken"),
            "qbo_realm_id": self.mapping.realm_id.id,
        }

    def _payment_journal(self, rec):
        """Best bank/cash/credit journal for a QBO payment."""
        Journal = self.env["account.journal"].with_company(self.mapping.company_id)
        journals = Journal.search(
            [
                ("company_id", "=", self.mapping.company_id.id),
                ("type", "in", ("bank", "cash", "credit")),
            ],
        )
        if not journals:
            return None

        account_ref = rec.get("DepositToAccountRef") or rec.get("BankAccountRef")
        detail = rec.get("CheckPayment") or rec.get("CreditCardPayment")
        if isinstance(detail, dict):
            account_ref = account_ref or detail.get("BankAccountRef")
        if account_ref:
            account = self._resolve_qbo_account(account_ref)
            if account:
                match = journals.filtered(lambda j: j.default_account_id == account)[:1]
                if match:
                    return match
            return None
        return journals[:1]

    def _upsert_journal_entries(self, qbo_records, direction="pull"):
        """Import grouped journal lines into account.move entries when possible.

        The QuickBooks ZIP reports do not always carry a full transactional
        payload, so this importer only creates balanced journal entries with
        resolvable accounts. Unbalanced or ambiguous rows are logged and
        skipped instead of forcing a bad accounting move.
        """
        if "poseidon.books.intake" in self.env:
            # Installed intake boundary preserves unresolved history for human review.
            staged = self.env["poseidon.books.intake"]._stage_qbo(self.mapping, qbo_records)
            for item in staged:
                item.action_prepare()
            return
        Move = self.env["account.move"].with_company(self.mapping.company_id)
        for rec in qbo_records:
            if self._is_live_journal_record(rec):
                self._upsert_live_journal_entry(rec, direction)

        report_records = [rec for rec in qbo_records if not self._is_live_journal_record(rec)]
        grouped = defaultdict(list)
        for rec in report_records:
            group_key = self._journal_group_key(rec)
            if not group_key:
                continue
            grouped[group_key].append(rec)

        for group_key, rows in grouped.items():
            t0 = time.monotonic()
            try:
                move_vals = self._build_journal_move_vals(rows)
                if not move_vals:
                    self._log(
                        "journal_entry",
                        direction,
                        "skipped",
                        "skip",
                        group_key,
                        None,
                        t0,
                        "Could not derive a balanced journal entry from the report rows",
                    )
                    continue
                move = Move.create(move_vals)
                self._log("journal_entry", direction, "success", "create", group_key, move, t0)
            except Exception as exc:
                self._log_upsert_exception("journal_entry", direction, group_key, None, t0, exc)

    def _is_live_journal_record(self, rec):
        for line in rec.get("Line") or []:
            if not isinstance(line, dict):
                continue
            if line.get("DetailType") == "JournalEntryLineDetail":
                return True
            if isinstance(line.get("JournalEntryLineDetail"), dict):
                return True
        return False

    def _upsert_live_journal_entry(self, rec, direction="pull"):
        """Import one live QBO JournalEntry as a draft, balanced account.move."""
        Move = self.env["account.move"].with_company(self.mapping.company_id)
        t0 = time.monotonic()
        qbo_id = rec.get("Id")
        try:
            with self.env.cr.savepoint():
                vals = self._build_live_journal_move_vals(rec)
                if vals:
                    vals["qbo_source_type"] = "journal_entry"
                if not vals:
                    self._log(
                        "journal_entry",
                        direction,
                        "skipped",
                        "skip",
                        qbo_id,
                        None,
                        t0,
                        "Could not derive a balanced journal entry from the API payload",
                    )
                    return
                existing = self._find_source_record("account.move", "journal_entry", qbo_id)
                if existing:
                    if existing.state == "draft":
                        vals["line_ids"] = [(5, 0, 0), *vals["line_ids"]]
                        existing.write(vals)
                        self._log("journal_entry", direction, "success", "update", qbo_id, existing, t0)
                    else:
                        self._log(
                            "journal_entry",
                            direction,
                            "skipped",
                            "skip",
                            qbo_id,
                            existing,
                            t0,
                            "Posted journal entry left unchanged (pull cannot modify a posted move)",
                        )
                else:
                    move = Move.create(vals)
                    self._log("journal_entry", direction, "success", "create", qbo_id, move, t0)
        except Exception as exc:
            self._log_upsert_exception("journal_entry", direction, qbo_id, None, t0, exc)

    def _build_live_journal_move_vals(self, rec):
        """Map a live QBO JournalEntry payload to account.move write values."""
        lines = []
        total_debit = total_credit = 0.0
        for line in rec.get("Line") or []:
            if not isinstance(line, dict):
                continue
            detail = line.get("JournalEntryLineDetail")
            if not isinstance(detail, dict):
                continue
            account_ref = detail.get("AccountRef") or line.get("AccountRef")
            account = self._resolve_qbo_account(account_ref)
            if not account:
                return None

            amount = abs(self._float_value(line.get("Amount")))
            if not amount:
                continue
            posting_type = detail.get("PostingType") or line.get("PostingType")
            if posting_type == "Credit":
                debit, credit = 0.0, amount
            elif posting_type == "Debit":
                debit, credit = amount, 0.0
            elif self._float_value(line.get("Amount")) < 0:
                debit, credit = 0.0, amount
            else:
                debit, credit = amount, 0.0

            line_vals = {
                "name": line.get("Description") or account.name,
                "account_id": account.id,
                "debit": debit,
                "credit": credit,
            }
            distribution = self._qbo_class_distribution(detail) or self._qbo_class_distribution(line)
            if distribution:
                line_vals["analytic_distribution"] = distribution
            lines.append((0, 0, line_vals))
            total_debit += debit
            total_credit += credit

        if len(lines) < 2 or round(total_debit - total_credit, 2) != 0:
            return None
        journal = self._default_journal()
        if not journal:
            return None
        return {
            "move_type": "entry",
            "date": rec.get("TxnDate"),
            "ref": rec.get("DocNumber") or "",
            "journal_id": journal.id,
            "company_id": self.mapping.company_id.id,
            "line_ids": lines,
            "qbo_id": rec.get("Id"),
            "qbo_sync_token": rec.get("SyncToken"),
            "qbo_realm_id": self.mapping.realm_id.id,
        }

    def _upsert_employees(self, qbo_records, direction="pull"):
        """Import QBO employees into hr.employee when the model is available."""
        if "hr.employee" not in self.env:
            for rec in qbo_records:
                t0 = time.monotonic()
                self._log(
                    "employee",
                    direction,
                    "skipped",
                    "skip",
                    rec.get("Id"),
                    None,
                    t0,
                    "hr.employee is not installed in this database",
                )
            return

        Employee = self.env["hr.employee"].with_company(self.mapping.company_id)
        for rec in qbo_records:
            t0 = time.monotonic()
            qbo_id = rec.get("Id") or rec.get("Name") or rec.get("DisplayName")
            try:
                vals = self._map_employee_to_odoo(rec)
                existing = Employee.search([("name", "=", vals["name"])], limit=1)
                if existing:
                    existing.write(vals)
                    self._log("employee", direction, "success", "update", qbo_id, existing, t0)
                else:
                    new_rec = Employee.create(vals)
                    self._log("employee", direction, "success", "create", qbo_id, new_rec, t0)
            except Exception as exc:
                self._log_upsert_exception("employee", direction, qbo_id, None, t0, exc)

    def _upsert_products(self, qbo_records, direction="pull"):
        """Create or update product.template records from QBO Item data."""
        Product = self.env["product.template"].with_company(self.mapping.company_id)
        for rec in qbo_records:
            t0 = time.monotonic()
            qbo_id = rec.get("Id")
            try:
                odoo_vals = self._map_product_to_odoo(rec)
                existing = Product.search([("qbo_id", "=", qbo_id)], limit=1) if qbo_id else None
                if existing:
                    if self._detect_conflict(existing, rec, "product"):
                        self._create_conflict(existing, rec, "product")
                        continue
                    existing.write(odoo_vals)
                    self._log("product", direction, "success", "update", qbo_id, existing, t0)
                else:
                    new_rec = Product.create(odoo_vals)
                    self._log("product", direction, "success", "create", qbo_id, new_rec, t0)
            except Exception as exc:
                self._log_upsert_exception("product", direction, qbo_id, None, t0, exc)

    # =========================================================================
    # Push helpers (Odoo → QBO) — DORMANT in the MVP
    # =========================================================================
    #
    # The MVP is hard pull-only: no scheduled, manual, BFF, or conflict path may
    # write to QuickBooks. These helpers are kept as a future implementation
    # surface ONLY. They are deliberately NOT called by ``sync_all``, the cron,
    # ``action_request_pull``, ``action_sync_now``, or conflict resolution.
    # Do not wire them back into the sync flow without a product decision to
    # ship two-way sync (and the matching TRL gate).

    def _push_accounts(self, since=None):
        """Dormant: push Odoo accounts modified after ``since`` that have no
        qbo_id yet, or whose write_date > last QBO update.

        Not invoked by the MVP pull-only flow."""
        domain = [("company_ids", "=", self.mapping.company_id.id), ("qbo_id", "=", False)]
        if since:
            domain.append(("write_date", ">=", since))
        if "poseidon_qbo_push_approved" in self.env["account.account"]._fields:
            domain.append(("poseidon_qbo_push_approved", "=", True))
        accounts = self.env["account.account"].search(domain)
        for acc in accounts:
            self.push_account_record(acc)

    def _push_partners(self, since=None):
        # TODO: push new/updated Odoo partners without a qbo_id
        pass

    def _push_invoices(self, since=None):
        # TODO: push new/updated Odoo invoices without a qbo_id
        pass

    def _push_products(self, since=None):
        # TODO: push new/updated Odoo products without a qbo_id
        pass

    # =========================================================================
    # Field mapping: QBO → Odoo
    # =========================================================================

    def _map_account_to_odoo(self, rec):
        """Map a QBO Account dict to account.account write values.

        TODO: refine account_type mapping to match your Kodoo CoA structure.
        """
        bridge_rule = self._match_account_bridge_rule(rec)
        qbo_type = rec.get("AccountType", "")
        account_type = _QBO_ACCOUNT_TYPE_MAP.get(qbo_type, "asset_current")
        vals = {
            "name": rec.get("Name", ""),
            "code": self._normalize_account_code(rec.get("AcctNum"), rec.get("Id")),
            "account_type": account_type,
            "note": rec.get("Description", ""),
            "active": rec.get("Active", True),
            "qbo_id": rec.get("Id"),
            "qbo_sync_token": rec.get("SyncToken"),
            "qbo_bridge_rule_id": bridge_rule.id if bridge_rule else False,
            "qbo_source_name": rec.get("Name", ""),
            "qbo_source_account_number": rec.get("AcctNum", ""),
            "qbo_source_account_type": qbo_type,
            "qbo_source_account_subtype": rec.get("AccountSubType", ""),
            "company_ids": [(4, self.mapping.company_id.id)],
        }
        if (
            bridge_rule
            and "standard_account_id" in bridge_rule._fields
            and bridge_rule.standard_account_id
            and "qbo_standard_account_id" in self.env["account.account"]._fields
        ):
            vals["qbo_standard_account_id"] = bridge_rule.standard_account_id.id
        return vals

    def _map_partner_to_odoo(self, rec):
        """Map QBO Customer or Vendor to res.partner values."""
        qbo_type = rec.get("_qbo_type", "customer")
        email_obj = rec.get("PrimaryEmailAddr", {})
        phone_obj = rec.get("PrimaryPhone", {})
        addr = rec.get("BillAddr", {})
        return {
            "name": rec.get("DisplayName") or rec.get("CompanyName", ""),
            "company_name": rec.get("CompanyName", ""),
            "email": email_obj.get("Address", "") if isinstance(email_obj, dict) else email_obj,
            "phone": phone_obj.get("FreeFormNumber", "") if isinstance(phone_obj, dict) else phone_obj,
            "street": addr.get("Line1", "") if isinstance(addr, dict) else "",
            "city": addr.get("City", "") if isinstance(addr, dict) else "",
            "zip": addr.get("PostalCode", "") if isinstance(addr, dict) else "",
            "customer_rank": 1 if qbo_type == "customer" else 0,
            "supplier_rank": 1 if qbo_type == "vendor" else 0,
            "active": rec.get("Active", True),
            "qbo_id": rec.get("Id"),
            "qbo_sync_token": rec.get("SyncToken"),
            "company_id": self.mapping.company_id.id,
        }

    def _map_invoice_to_odoo(self, rec):
        """Map a QBO Invoice or Bill to a reviewable Odoo draft."""
        qbo_type = rec.get("_qbo_type", "invoice")
        if qbo_type not in ("invoice", "bill"):
            raise UserError("This source document type requires a supported translation.")
        move_type = "out_invoice" if qbo_type == "invoice" else "in_invoice"
        journal_type = "sale" if qbo_type == "invoice" else "purchase"
        journal = self.env["account.journal"].with_company(self.mapping.company_id).search([
            ("company_id", "=", self.mapping.company_id.id),
            ("type", "=", journal_type),
        ], limit=1)
        if not journal:
            raise UserError("No journal is configured for QBO %s imports." % qbo_type)

        partner_ref = rec.get("CustomerRef") if qbo_type == "invoice" else rec.get("VendorRef")
        partner_id, partner_name = self._qbo_ref(partner_ref)
        partner_domain = [
            ("company_id", "in", [False, self.mapping.company_id.id]),
            ("qbo_id", "=", partner_id),
        ]
        partner = partner_id and self.env["res.partner"].search(partner_domain, limit=1)
        if not partner:
            raise UserError(
                "No partner could be found for QBO %s. Sync customers and vendors first."
                % (partner_name or partner_id or rec.get("Id") or "document")
            )

        invoice_lines = self._map_invoice_lines(rec, qbo_type)
        if not invoice_lines:
            raise UserError("No account-backed lines could be found on this QBO document.")
        return {
            **self._invoice_conversion_values(rec),
            "move_type": move_type,
            "ref": rec.get("DocNumber"),
            "invoice_date": rec.get("TxnDate"),
            "invoice_date_due": rec.get("DueDate"),
            "partner_id": partner.id,
            "journal_id": journal.id,
            "invoice_line_ids": invoice_lines,
            "qbo_id": rec.get("Id"),
            "qbo_sync_token": rec.get("SyncToken"),
            "company_id": self.mapping.company_id.id,
        }

    def _map_invoice_lines(self, rec, qbo_type):
        commands = []
        for line in rec.get("Line") or []:
            amount = self._float_value(line.get("Amount"))
            if not amount:
                continue
            detail_type = line.get("DetailType") or ""
            if detail_type == "SubTotalLineDetail":
                # QBO rollup line duplicates the item lines above it and
                # carries no account or item of its own; skip it instead of
                # aborting the whole document.
                continue
            detail = line.get(detail_type) if isinstance(line.get(detail_type), dict) else {}
            account_ref = detail.get("AccountRef") or line.get("AccountRef")
            _account_id, account_name = self._qbo_ref(account_ref)
            account = self._resolve_qbo_account(account_ref)

            item_id, item_name = self._qbo_ref(detail.get("ItemRef"))
            Product = self.env["product.template"].with_company(self.mapping.company_id)
            product = item_id and Product.search([
                ("qbo_id", "=", item_id),
            ], limit=1)
            if not product and item_name:
                product = Product.search([
                    ("name", "=ilike", item_name),
                ], limit=1)
            if product and not account:
                if qbo_type == "invoice":
                    account = product.property_account_income_id or product.categ_id.property_account_income_categ_id
                else:
                    account = product.property_account_expense_id or product.categ_id.property_account_expense_categ_id
            if not account:
                raise UserError(
                    "No account could be found for QBO line %s. Sync accounts and products first."
                    % (line.get("Description") or item_name or account_name or "line")
                )

            quantity = self._float_value(detail.get("Qty")) or 1.0
            tax_detail = rec.get("TxnTaxDetail") or {}
            tax_code = self._invoice_tax_reference(rec, detail)
            tax_mapping = self.env["qbo.tax.mapping"].search([
                ("company_id", "=", self.mapping.company_id.id), ("realm_id", "=", self.mapping.realm_id.id),
                ("source_tax_code", "=", tax_code), ("direction", "=", "sale" if qbo_type == "invoice" else "purchase")], limit=2)
            taxes = tax_mapping.tax_ids if len(tax_mapping) == 1 else self.env["account.tax"]
            values = {"tax_ids": [fields.Command.set(taxes.ids)],
                "name": line.get("Description") or item_name or account_name or account.name,
                "account_id": account.id,
                "quantity": quantity,
                "price_unit": amount / quantity,
            }
            if product:
                values["product_id"] = product.product_variant_id.id
            distribution = self._qbo_class_distribution(line) or self._qbo_class_distribution(rec)
            if distribution:
                values["analytic_distribution"] = distribution
            commands.append((0, 0, values))
        return commands

    def _qbo_ref(self, value):
        if isinstance(value, dict):
            return str(value.get("value") or "").strip(), str(value.get("name") or "").strip()
        text = str(value or "").strip()
        return text, text

    def _resolve_qbo_account(self, ref):
        account_id, account_name = self._qbo_ref(ref)
        Account = self.env["account.account"].with_company(self.mapping.company_id)
        account = Account.search(
            [
                ("company_ids", "=", self.mapping.company_id.id),
                ("qbo_id", "=", account_id),
            ],
            limit=1,
        ) if account_id else Account.browse()
        if not account and account_name:
            account = Account.search(
                [
                    ("company_ids", "=", self.mapping.company_id.id),
                    ("name", "=ilike", account_name),
                ],
                limit=1,
            )
        return qbo_operational_account(self.env, self.mapping.company_id, account)

    def _map_product_to_odoo(self, rec):
        """Map a QBO Item to product.template values."""
        income_account = self._resolve_account_ref(rec.get("IncomeAccountRef"))
        expense_account = self._resolve_account_ref(rec.get("ExpenseAccountRef"))
        return {
            "name": rec.get("Name", ""),
            "description_sale": rec.get("Description", ""),
            "list_price": float(rec.get("UnitPrice", 0.0)),
            "standard_price": float(rec.get("PurchaseCost", 0.0)),
            "active": rec.get("Active", True),
            "default_code": rec.get("Sku", ""),
            "qbo_id": rec.get("Id"),
            "qbo_sync_token": rec.get("SyncToken"),
            "property_account_income_id": income_account,
            "property_account_expense_id": expense_account,
        }

    def _resolve_account_ref(self, ref):
        """Resolve a QBO AccountRef to the Odoo account id for this company."""
        account = self._resolve_qbo_account(ref)
        return account.id or False

    def _map_employee_to_odoo(self, rec):
        """Map a QBO employee row to hr.employee values."""
        name = rec.get("Name") or " ".join(
            part for part in [rec.get("GivenName"), rec.get("FamilyName")] if part
        ).strip()
        return {
            "name": name or rec.get("DisplayName") or rec.get("Id") or "Employee",
            "work_email": rec.get("PrimaryEmailAddr", {}).get("Address", "") if isinstance(rec.get("PrimaryEmailAddr"), dict) else rec.get("PrimaryEmailAddr", ""),
            "work_phone": rec.get("PrimaryPhone", {}).get("FreeFormNumber", "") if isinstance(rec.get("PrimaryPhone"), dict) else rec.get("PrimaryPhone", ""),
            "company_id": self.mapping.company_id.id,
        }

    # =========================================================================
    # Field mapping: Odoo → QBO
    # =========================================================================

    def _odoo_account_to_qbo(self, acc):
        """Map an Odoo account.account to a QBO Account create payload."""
        qbo_type = _ODOO_ACCOUNT_TYPE_MAP.get(acc.account_type, "Other Asset")
        return {
            "Name": acc.name,
            "AccountType": qbo_type,
            "AcctNum": acc.code or "",
            "Description": acc.note or "",
            "Active": acc.active,
        }

    def _match_account_bridge_rule(self, rec):
        return self.env["qbo.account.bridge.rule"].with_company(self.mapping.company_id).match_qbo_record(rec)

    def _journal_group_key(self, rec):
        date_value = rec.get("TxnDate") or rec.get("Date") or rec.get("transaction date")
        doc_number = rec.get("DocNumber") or rec.get("Num") or rec.get("JournalEntryId") or rec.get("Transaction No")
        name = rec.get("Name") or rec.get("Payee") or rec.get("Customer") or rec.get("Vendor")
        memo = rec.get("Memo") or rec.get("Description") or rec.get("Memo/Description")
        source_file = rec.get("_source_file") or "qbo_package"
        if not any([date_value, doc_number, name, memo]):
            return None
        return (source_file, date_value or "", doc_number or "", name or "", memo or "")

    def _build_journal_move_vals(self, rows):
        lines = []
        date_value = False
        ref = ""
        for rec in rows:
            account = self._resolve_import_account(rec)
            debit, credit = self._extract_journal_amounts(rec)
            if not account or (not debit and not credit):
                continue
            date_value = date_value or rec.get("TxnDate") or rec.get("Date")
            ref = ref or rec.get("DocNumber") or rec.get("Num") or rec.get("Name") or rec.get("_source_file")
            line_name = rec.get("Memo") or rec.get("Description") or rec.get("Name") or ""
            line_vals = {
                "name": line_name,
                "account_id": account.id,
                "debit": debit,
                "credit": credit,
            }
            analytic_distribution = self._qbo_class_distribution(rec)
            if analytic_distribution:
                line_vals["analytic_distribution"] = analytic_distribution
            lines.append(
                (
                    0,
                    0,
                    line_vals,
                ),
            )

        if len(lines) < 2:
            return None

        total_debit = sum(line[2]["debit"] for line in lines)
        total_credit = sum(line[2]["credit"] for line in lines)
        if round(total_debit - total_credit, 2) != 0:
            return None

        journal = self._default_journal()
        if not journal:
            return None

        return {
            "move_type": "entry",
            "date": date_value,
            "ref": ref,
            "journal_id": journal.id,
            "company_id": self.mapping.company_id.id,
            "line_ids": lines,
        }

    def _qbo_class_distribution(self, rec):
        """Map an optional QBO Class to an existing Meridian cost center."""
        accounts = self.env["account.analytic.account"]
        if "poseidon_cost_center_external_ref" not in accounts._fields:
            return {}
        class_ref = rec.get("ClassRef") or rec.get("Class") or rec.get("Class Name")
        for detail_name in (
            "JournalEntryLineDetail",
            "AccountBasedExpenseLineDetail",
            "ItemBasedExpenseLineDetail",
            "SalesItemLineDetail",
        ):
            detail = rec.get(detail_name)
            if not class_ref and isinstance(detail, dict):
                class_ref = detail.get("ClassRef")
        if not class_ref:
            return {}
        if isinstance(class_ref, dict):
            external_ref = str(class_ref.get("value") or "").strip()
            name = str(class_ref.get("name") or "").strip()
        else:
            external_ref = name = str(class_ref).strip()
        plan = self.env.ref(
            "poseidon_cost_center.analytic_plan_cost_center",
            raise_if_not_found=False,
        )
        if not plan:
            return {}
        domain = [
            ("root_plan_id", "=", plan.id),
            ("company_id", "in", [False, self.mapping.company_id.id]),
        ]
        center = external_ref and accounts.search(
            domain + [("poseidon_cost_center_external_ref", "=", external_ref)],
            limit=1,
        )
        if not center and name:
            center = accounts.search(domain + [("code", "=", name)], limit=1)
        if not center and name:
            center = accounts.search(domain + [("name", "=", name)], limit=1)
        return {str(center.id): 100.0} if center else {}

    def _extract_journal_amounts(self, rec):
        debit = self._float_value(rec.get("Debit"))
        credit = self._float_value(rec.get("Credit"))
        if not debit and not credit:
            amount = self._float_value(rec.get("Amount"))
            if amount > 0:
                debit = amount
            elif amount < 0:
                credit = abs(amount)
        return debit, credit

    def _resolve_import_account(self, rec):
        account_label = (
            rec.get("Account")
            or rec.get("Split")
            or rec.get("Category")
            or rec.get("Account Name")
            or rec.get("Name")
            or ""
        )
        account_code = rec.get("Account Number") or rec.get("AcctNum") or rec.get("Code")
        domain = [("company_ids", "=", self.mapping.company_id.id)]
        Account = self.env["account.account"].with_company(self.mapping.company_id)
        search_order = []
        if account_code:
            search_order.append([("code", "=", str(account_code).strip())])
        if account_label:
            search_order.append([("name", "=ilike", str(account_label).strip())])
            search_order.append([("qbo_source_name", "=ilike", str(account_label).strip())])
        for extra_domain in search_order:
            account = Account.search(domain + extra_domain, limit=1)
            if account:
                return qbo_operational_account(self.env, self.mapping.company_id, account)
        return None

    def _default_journal(self):
        Journal = self.env["account.journal"].with_company(self.mapping.company_id)
        return Journal.search(
            [("company_id", "=", self.mapping.company_id.id), ("type", "=", "general")],
            limit=1,
        )

    def _float_value(self, value):
        if value in (None, False, ""):
            return 0.0
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip().replace(",", "")
        try:
            return float(text)
        except ValueError:
            return 0.0

    def push_account_record(self, account):
        t0 = time.monotonic()
        was_existing = bool(account.qbo_id)
        try:
            payload = self._odoo_account_to_qbo(account)
            if account.qbo_id:
                payload.update(
                    {
                        "Id": account.qbo_id,
                        "SyncToken": account.qbo_sync_token or "0",
                        "sparse": True,
                    },
                )
                result = self.client.update_account(payload)
            else:
                result = self.client.create_account(payload)

            write_vals = {
                "qbo_id": result.get("Id"),
                "qbo_sync_token": result.get("SyncToken"),
                "qbo_last_sync": fields.Datetime.now(),
            }
            if "qbo_realm_id" in account._fields:
                write_vals["qbo_realm_id"] = self.mapping.realm_id.id
            if (
                "qbo_standard_account_id" in account._fields
                and "standard_account_id" in self.env["qbo.account.bridge.rule"]._fields
                and account.qbo_bridge_rule_id.standard_account_id
            ):
                write_vals["qbo_standard_account_id"] = (
                    account.qbo_bridge_rule_id.standard_account_id.id
                )

            account.sudo().write(write_vals)
            self._log(
                "account", "push", "success",
                "update" if was_existing else "create",
                result.get("Id"), account, t0,
            )
            return result
        except Exception as exc:
            _logger.exception("Push account %s failed", account.display_name)
            self._log(
                "account",
                "push",
                "error",
                "error",
                account.qbo_id,
                account,
                t0,
                str(exc),
            )
            raise

    # =========================================================================
    # Conflict detection
    # =========================================================================

    def _detect_conflict(self, odoo_record, qbo_record, entity_type):
        """Return True if both sides have changed since last sync.

        Logic: odoo_record.write_date > last_sync AND qbo_record.MetaData.LastUpdatedTime > last_sync
        """
        last_sync = self.mapping.get_last_sync_for(entity_type)
        if not last_sync:
            return False  # First sync — no conflict possible

        # QBO last update
        meta = qbo_record.get("MetaData", {})
        qbo_updated_str = meta.get("LastUpdatedTime", "")
        if not qbo_updated_str:
            return False
        qbo_updated = self._parse_qbo_datetime(qbo_updated_str)
        if not qbo_updated:
            return False

        odoo_write = getattr(odoo_record, "write_date", None)
        if not odoo_write:
            return False

        # Both sides changed after last sync → conflict
        return odoo_write > last_sync and qbo_updated > last_sync

    def _create_conflict(self, odoo_record, qbo_record, entity_type):
        """Create a qbo.conflict record for manual review."""
        meta = qbo_record.get("MetaData", {})
        qbo_last_updated = self._parse_qbo_datetime(meta.get("LastUpdatedTime"))

        # Build a lightweight Odoo snapshot (only conflict-relevant fields)
        odoo_snapshot = {}
        for fname in _CONFLICT_FIELDS.get(entity_type, []):
            with suppress(Exception):
                odoo_snapshot[fname] = str(getattr(odoo_record, fname.lower(), ""))

        self.env["qbo.conflict"].sudo().create({
            "mapping_id": self.mapping.id,
            "entity_type": entity_type,
            "qbo_id": qbo_record.get("Id"),
            "odoo_model": _ODOO_MODELS.get(entity_type),
            "odoo_record_id": odoo_record.id,
            "odoo_data": json.dumps(odoo_snapshot),
            "qbo_data": json.dumps({k: qbo_record.get(k) for k in _CONFLICT_FIELDS.get(entity_type, [])}),
            "odoo_write_date": getattr(odoo_record, "write_date", False),
            "qbo_last_updated": fields.Datetime.to_string(qbo_last_updated) if qbo_last_updated else False,
        })
        self._stats["conflicts"] += 1
        QboSyncLog.log(
            self.env, self.mapping, entity_type, "conflict", "conflict", "conflict",
            qbo_id=qbo_record.get("Id"),
            odoo_model=_ODOO_MODELS.get(entity_type),
            odoo_record_id=odoo_record.id,
            message="Both sides modified since last sync — flagged for review",
        )

    # =========================================================================
    # Utilities
    # =========================================================================

    def _safe_sync(self, entity_type, fn):
        try:
            fn()
        except QBOApiError as exc:
            _logger.error("QBO API error syncing %s: %s", entity_type, exc)
            self.mapping.realm_id._set_error(str(exc))
        except Exception:
            _logger.exception("Unexpected error syncing %s", entity_type)

    def _log(self, entity_type, direction, status, operation, qbo_id, odoo_record, t0, message=None):
        duration_ms = int((time.monotonic() - t0) * 1000)
        QboSyncLog.log(
            self.env, self.mapping, entity_type, direction, status, operation,
            qbo_id=qbo_id,
            odoo_model=_ODOO_MODELS.get(entity_type) if odoo_record else None,
            odoo_record_id=odoo_record.id if odoo_record else None,
            message=message,
            duration_ms=duration_ms,
        )
        # Count by operation, not status: "success" is not a counter, and
        # bucketing it would leave created/updated permanently at zero.
        self._stats[_OPERATION_STATS.get(operation, "skipped")] += 1

    def _log_upsert_exception(self, entity_type, direction, qbo_id, odoo_record, t0, exc):
        message = str(exc)
        if self._is_missing_local_prerequisite(exc):
            _logger.warning("%s skipped for QBO ID %s: %s", entity_type, qbo_id, message)
            self._log(entity_type, direction, "skipped", "skip", qbo_id, odoo_record, t0, message)
            return
        _logger.exception("%s upsert failed for QBO ID %s", entity_type, qbo_id)
        self._log(entity_type, direction, "error", "error", qbo_id, odoo_record, t0, message)

    def _is_missing_local_prerequisite(self, exc):
        if not isinstance(exc, (UserError, ValidationError)):
            return False
        message = str(exc).lower()
        return any(
            marker in message
            for marker in (
                "could be found",
                "not configured",
                "is missing",
                "missing",
                "no journal",
                "no account",
                "no partner",
                "no tax",
            )
        )

    def _normalize_account_code(self, raw_code, qbo_id):
        for candidate in (raw_code, qbo_id):
            if not candidate:
                continue
            sanitized = re.sub(r"[^A-Za-z0-9.]+", "", str(candidate)).strip(".")
            if sanitized:
                return sanitized[:64]
        return False

    def _parse_qbo_datetime(self, value):
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed



    def _find_source_record(self, model, source_type, source_id):
        if not source_id:
            raise UserError("A source record ID is required.")
        Model = self.env[model].with_company(self.mapping.company_id)
        base = [("company_id", "=", self.mapping.company_id.id), ("qbo_id", "=", str(source_id))]
        records = Model.search(base + [("qbo_realm_id", "=", self.mapping.realm_id.id),
                                      ("qbo_source_type", "=", source_type)], limit=2)
        if len(records) > 1:
            raise UserError("Multiple source records require review.")
        legacy = Model.search(base + ["|", ("qbo_realm_id", "=", False), ("qbo_source_type", "=", False)], limit=1)
        if legacy:
            raise UserError("Legacy source identity requires explicit connection review.")
        return records

    def _invoice_tax_reference(self, rec, detail):
        # Intuit's US taxable line marker is distinct from the transaction tax code.
        line_code, _name = self._qbo_ref(detail.get("TaxCodeRef"))
        document_code, _name = self._qbo_ref((rec.get("TxnTaxDetail") or {}).get("TxnTaxCodeRef"))
        if line_code == "TAX" and document_code:
            return document_code
        return line_code or document_code

    def _invoice_conversion_values(self, rec):
        import math
        tax_detail = rec.get("TxnTaxDetail") or {}
        if not isinstance(tax_detail, dict):
            raise UserError("Invalid source tax evidence.")
        def amount(value):
            return type(value) in (int, float) and math.isfinite(value)
        currency_ref = rec.get("CurrencyRef") or {}
        currency = currency_ref.get("value") if isinstance(currency_ref, dict) else None
        codes = []
        signatures = []
        reasons = []
        for line in rec.get("Line") or []:
            if line.get("DetailType") == "SubTotalLineDetail" or not self._float_value(line.get("Amount")):
                continue
            if line.get("DetailType") in ("DiscountLineDetail", "GroupLineDetail"):
                reasons.append("This source discount/group shape requires review.")
            detail = line.get(line.get("DetailType")) or {}
            code = self._invoice_tax_reference(rec, detail)
            codes.append(code)
            mapping = self.env["qbo.tax.mapping"].search([
                ("company_id", "=", self.mapping.company_id.id), ("realm_id", "=", self.mapping.realm_id.id),
                ("source_tax_code", "=", code),
                ("direction", "=", "sale" if rec.get("_qbo_type", "invoice") == "invoice" else "purchase")], limit=2)
            signatures.append(self.env["account.move"]._qbo_tax_signature(mapping.tax_ids))
        return {"qbo_realm_id": self.mapping.realm_id.id, "qbo_source_type": rec.get("_qbo_type", "invoice"),
            "qbo_conversion_evidence": {"total_present": amount(rec.get("TotalAmt")),
                "tax_present": amount(tax_detail.get("TotalTax")), "total": rec.get("TotalAmt") if amount(rec.get("TotalAmt")) else None,
                "tax": tax_detail.get("TotalTax") if amount(tax_detail.get("TotalTax")) else None,
                "currency": currency, "tax_codes": codes, "tax_configuration": signatures, "reasons": reasons,
                "tax_calculation": rec.get("GlobalTaxCalculation"), "source_tax_detail": tax_detail}}

    def _payment_reference_values(self, rec, journal):
        import math
        evidence = []
        reasons = []
        ids = []
        allocated = 0.0
        payment_detail = rec.get("CheckPayment") or rec.get("CreditCardPayment") or {}
        source_bank = rec.get("DepositToAccountRef") or rec.get("BankAccountRef") or (
            payment_detail.get("BankAccountRef") if isinstance(payment_detail, dict) else None)
        if not source_bank:
            reasons.append("Source payment journal reference requires review.")
        currency = journal.currency_id or self.mapping.company_id.currency_id
        if currency != self.mapping.company_id.currency_id:
            reasons.append("Foreign-currency payments require reviewed conversion evidence.")
        partner_ref = rec.get("VendorRef") if rec.get("_qbo_type") == "bill_payment" else rec.get("CustomerRef")
        partner_id, _name = self._qbo_ref(partner_ref)
        allowed_type = "Bill" if rec.get("_qbo_type") == "bill_payment" else "Invoice"
        for line in rec.get("Line") or []:
            links = line.get("LinkedTxn") or []
            if not links:
                continue
            value = line.get("Amount")
            if len(links) != 1 or type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                reasons.append("Payment allocation is missing or ambiguous.")
                evidence.append({"amount": value, "links": links})
                continue
            allocated += value
            for link in links:
                evidence.append({"amount": value, "source_id": link.get("TxnId"), "source_type": link.get("TxnType")})
                if link.get("TxnType") != allowed_type:
                    reasons.append("Payment reference has an incompatible document direction.")
                    continue
                try:
                    moves = self._find_source_record("account.move", "bill" if allowed_type == "Bill" else "invoice", link.get("TxnId"))
                except UserError as exc:
                    reasons.append(str(exc))
                    continue
                if not moves or moves.partner_id.qbo_id != partner_id:
                    reasons.append("Payment document reference is missing or has a different partner.")
                    continue
                ids.extend(moves.ids)
        total = rec.get("TotalAmt", rec.get("Amount"))
        ref = rec.get("CurrencyRef") or {}
        source_currency = ref.get("value") if isinstance(ref, dict) else None
        if source_currency != currency.name:
            reasons.append("Source payment currency requires review.")
        valid_total = type(total) in (int, float) and math.isfinite(total) and total >= 0
        if not valid_total or currency.compare_amounts(allocated, total) > 0:
            reasons.append("Payment allocations exceed or lack a valid source amount.")
        if rec.get("LinkedTxn"):
            reasons.append("Document-level payment references lack per-document allocation evidence.")
            evidence.append({"links": rec["LinkedTxn"]})
        return {"qbo_realm_id": self.mapping.realm_id.id, "qbo_source_type": rec.get("_qbo_type", "payment"),
            "qbo_link_evidence": {"allocations": evidence, "source_currency": source_currency,
                "native_currency": currency.name, "source_total": total if valid_total else None, "source_bank": source_bank, "journal_id": journal.id}, "qbo_link_reasons": list(dict.fromkeys(reasons)),
            "qbo_linked_move_ids": [fields.Command.set(list(dict.fromkeys(ids)))],
            "qbo_unapplied_amount": max(total - allocated, 0) if valid_total and not reasons else 0,
            "qbo_link_state": "review" if reasons else "validated"}

# ── Account type translation tables ──────────────────────────────────────────

_QBO_ACCOUNT_TYPE_MAP = {
    "Bank": "asset_cash",
    "Accounts Receivable": "asset_receivable",
    "Other Current Asset": "asset_current",
    "Fixed Asset": "asset_fixed",
    "Other Asset": "asset_non_current",
    "Accounts Payable": "liability_payable",
    "Credit Card": "liability_credit_card",
    "Other Current Liability": "liability_current",
    "Long Term Liability": "liability_non_current",
    "Equity": "equity",
    "Income": "income",
    "Cost of Goods Sold": "expense_direct_cost",
    "Expense": "expense",
    "Other Income": "income_other",
    "Other Expense": "expense_other",
}

_ODOO_ACCOUNT_TYPE_MAP = {v: k for k, v in _QBO_ACCOUNT_TYPE_MAP.items()}
