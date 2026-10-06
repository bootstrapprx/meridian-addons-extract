"""Conversion integrity with synthetic 16x9-style records, never live QBO."""
from unittest.mock import patch
from odoo import fields
from odoo.exceptions import UserError, ValidationError
from .test_qbo_sync_engine import TestQBOSyncEngineInvoices


class TestQBOConversionIntegrity(TestQBOSyncEngineInvoices):
    def setUp(self):
        super().setUp()
        self.company.name = "16x9 LLC (synthetic test entity)"
        self.env.ref("base.USD").active = True
        self.company.currency_id = self.env.ref("base.USD")
        self.company.country_id = self.env.ref("base.us")
        self.company.account_fiscal_country_id = self.env.ref("base.us")

    def _source(self, source_id="16X9-SYNTHETIC-INVOICE", code="TEST-TAX", total=110):
        return {
            "Id": source_id, "_qbo_type": "invoice", "SyncToken": "0",
            "TxnDate": "2026-10-05", "CustomerRef": {"value": "QBO-CUSTOMER"},
            "CurrencyRef": {"value": self.company.currency_id.name},
            "TotalAmt": total, "TxnTaxDetail": {"TotalTax": 10},
            "GlobalTaxCalculation": "TaxExcluded",
            "Line": [{"Amount": 100, "DetailType": "SalesItemLineDetail",
                      "SalesItemLineDetail": {"Qty": 1, "ItemRef": {"value": "QBO-ITEM"},
                                              "TaxCodeRef": {"value": code}}}],
        }

    def _tax_map(self, code="TEST-TAX", taxes=None):
        if taxes is None:
            taxes = self.env["account.tax"].create({"name": "Synthetic test tax",
                "company_id": self.company.id, "tax_group_id": self.env["account.tax.group"].create({"name": "Synthetic group", "company_id": self.company.id}).id, "amount_type": "percent", "amount": 10,
                "type_tax_use": "sale", "price_include_override": "tax_excluded"})
        return self.env["qbo.tax.mapping"].create({"company_id": self.company.id,
            "realm_id": self.realm.id, "source_tax_code": code, "direction": "sale",
            "tax_ids": [fields.Command.set(taxes.ids)]})

    def _move(self, source_id="16X9-SYNTHETIC-INVOICE"):
        return self.env["account.move"].search([("company_id", "=", self.company.id),
            ("qbo_id", "=", source_id)])

    def test_mapped_tax_and_native_total_are_preserved(self):
        mapping = self._tax_map()
        self._engine()._upsert_invoices([self._source()])
        move = self._move()
        self.assertTrue(move)
        self.assertEqual(move.amount_untaxed, 100)
        self.assertEqual(move.amount_tax, 10)
        self.assertEqual(move.amount_total, 110)
        self.assertEqual(move.invoice_line_ids.tax_ids, mapping.tax_ids)
        self.assertEqual(move.qbo_conversion_state, "validated")
        self.assertEqual(move.qbo_realm_id, self.realm)
        self.assertEqual(move.qbo_source_type, "invoice")

    def test_missing_map_is_review_and_cannot_post(self):
        self._engine()._upsert_invoices([self._source()])
        move = self._move()
        self.assertTrue(move)
        self.assertEqual(move.qbo_conversion_state, "review")
        with self.assertRaises(UserError):
            move.action_post()
        self.assertEqual(move.state, "draft")

    def test_source_mismatch_and_manual_edit_block_posting(self):
        self._tax_map()
        self._engine()._upsert_invoices([self._source(total=999)])
        move = self._move()
        with self.assertRaises(UserError):
            move.action_post()
        self._engine()._upsert_invoices([self._source()])
        move.invoice_line_ids.price_unit = 90
        with self.assertRaises(UserError):
            move.action_post()

    def test_map_deactivation_invalidates_conversion(self):
        mapping = self._tax_map()
        self._engine()._upsert_invoices([self._source()])
        mapping.active = False
        with self.assertRaises(UserError):
            self._move().action_post()

    def test_repeat_import_and_legacy_identity(self):
        self._tax_map()
        source = self._source()
        self._engine()._upsert_invoices([source, source])
        self.assertEqual(len(self._move()), 1)
        move = self._move()
        move.qbo_realm_id = False
        self._engine()._upsert_invoices([{**source, "TotalAmt": 999}])
        self.assertEqual(len(self._move()), 1)
        self.assertEqual(move.amount_total, 110)

    def test_tax_mapping_refuses_cross_company_and_unconfirmed_writes(self):
        foreign = self.env["res.company"].create({"name": "Synthetic foreign company", "country_id": self.env.ref("base.us").id, "account_fiscal_country_id": self.env.ref("base.us").id})
        tax = self.env["account.tax"].create({"name": "Foreign synthetic tax", "company_id": foreign.id,
            "tax_group_id": self.env["account.tax.group"].create({"name": "Foreign group", "company_id": foreign.id}).id, "amount_type": "percent", "amount": 10, "type_tax_use": "sale"})
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self._tax_map(taxes=tax)
        with self.assertRaises(UserError):
            self.env["qbo.tax.mapping"].apply_configuration(self.company.id, [], False)

    def test_payment_links_are_evidence_without_posting_or_reconciliation(self):
        self._tax_map()
        self._engine()._upsert_invoices([self._source()])
        source = {"Id": "16X9-SYNTHETIC-PAYMENT", "TotalAmt": 110, "TxnDate": "2026-10-05",
            "CustomerRef": {"value": "QBO-CUSTOMER"}, "CurrencyRef": {"value": self.company.currency_id.name},
            "DepositToAccountRef": {"value": "QBO-BANK"},
            "Line": [{"Amount": 100, "LinkedTxn": [{"TxnId": "16X9-SYNTHETIC-INVOICE", "TxnType": "Invoice"}]}]}
        with patch.object(type(self.env["account.payment"]), "action_post", side_effect=AssertionError("Unexpected post")):
            self._engine()._upsert_payments([source, source])
        payment = self.env["account.payment"].search([("qbo_id", "=", source["Id"]), ("company_id", "=", self.company.id)])
        self.assertEqual(len(payment), 1)
        self.assertEqual(payment.state, "draft")
        self.assertEqual(payment.qbo_linked_move_ids, self._move())
        self.assertEqual(payment.qbo_unapplied_amount, 10)
        self.assertEqual(payment.qbo_link_state, "validated")
        self.assertFalse(payment.invoice_ids)
        self.assertEqual(self._move().state, "draft")

    def test_explicit_zero_tax_clears_product_default_taxes(self):
        default = self.env["account.tax"].create({"name": "Default synthetic tax", "company_id": self.company.id,
            "amount_type": "percent", "amount": 10, "type_tax_use": "sale",
            "tax_group_id": self.env["account.tax.group"].create({"name": "Synthetic group", "company_id": self.company.id}).id})
        self.product.taxes_id = default
        self._tax_map(code="ZERO", taxes=self.env["account.tax"])
        source = self._source(code="ZERO", total=100)
        source["TxnTaxDetail"]["TotalTax"] = 0
        self._engine()._upsert_invoices([source])
        move = self._move()
        self.assertFalse(move.invoice_line_ids.tax_ids)
        self.assertEqual(move.amount_total, 100)
        self.assertEqual(move.qbo_conversion_state, "validated")

    def test_identity_separates_realms_and_rejects_legacy_adoption(self):
        self._tax_map()
        self._engine()._upsert_invoices([self._source()])
        original = self._move()
        other = self.env["qbo.realm"].create({"name": "Synthetic other realm", "realm_id": "synthetic-other"})
        original.qbo_realm_id = other
        self._engine()._upsert_invoices([self._source()])
        self.assertEqual(len(self._move()), 2)
        self.assertEqual(original.qbo_realm_id, other)
        current = self._move().filtered(lambda move: move.qbo_realm_id == self.realm)
        current.qbo_source_type = False
        self._engine()._upsert_invoices([self._source(total=777)])
        self.assertEqual(len(self._move()), 2)
        self.assertEqual(current.amount_total, 110)

    def test_rejected_write_rolls_back_and_next_record_still_imports(self):
        self._tax_map()
        self._engine()._upsert_invoices([self._source()])
        move = self._move()
        validate = type(move)._qbo_validate_conversion
        def fail_one(records):
            if records.qbo_id == "16X9-SYNTHETIC-INVOICE":
                raise UserError("Synthetic conversion failure after lines were replaced")
            return validate(records)
        bad = self._source(total=220)
        bad["Line"][0]["Amount"] = 200
        good = self._source(source_id="16X9-NEXT-INVOICE")
        with patch.object(type(move), "_qbo_validate_conversion", fail_one):
            self._engine()._upsert_invoices([bad, good])
        move.invalidate_recordset()
        self.assertEqual(move.amount_total, 110)
        self.assertTrue(self._move("16X9-NEXT-INVOICE"))

    def test_missing_total_and_foreign_currency_require_review(self):
        self._tax_map()
        source = self._source()
        del source["TotalAmt"]
        source["CurrencyRef"] = {"value": "SYNTHETIC-FOREIGN"}
        self._engine()._upsert_invoices([source])
        move = self._move()
        self.assertEqual(move.qbo_conversion_state, "review")
        with self.assertRaises(UserError):
            move.action_post()

    def test_payment_missing_document_and_overallocation_require_review(self):
        source = {"Id": "16X9-SYNTHETIC-PAYMENT", "TotalAmt": 100, "TxnDate": "2026-10-05",
            "CustomerRef": {"value": "QBO-CUSTOMER"}, "CurrencyRef": {"value": self.company.currency_id.name},
            "DepositToAccountRef": {"value": "QBO-BANK"},
            "Line": [{"Amount": 120, "LinkedTxn": [{"TxnId": "MISSING", "TxnType": "Invoice"}]}]}
        self._engine()._upsert_payments([source])
        payment = self.env["account.payment"].search([("qbo_id", "=", source["Id"]), ("company_id", "=", self.company.id)])
        self.assertEqual(payment.qbo_link_state, "review")
        self.assertFalse(payment.qbo_linked_move_ids)
        self.assertEqual(payment.state, "draft")
        self.assertTrue(payment.qbo_link_reasons)

    def test_changed_canonical_tax_direction_invalidates_posting(self):
        mapping = self._tax_map()
        self._engine()._upsert_invoices([self._source()])
        mapping.tax_ids.type_tax_use = "purchase"
        self._move()._qbo_validate_conversion()
        self.assertEqual(self._move().qbo_conversion_state, "review")
        with self.assertRaises(UserError):
            self._move().action_post()

    def test_legacy_reference_preserves_payment_evidence_for_review(self):
        self._tax_map()
        self._engine()._upsert_invoices([self._source()])
        self._move().qbo_realm_id = False
        source = {"Id": "16X9-SYNTHETIC-PAYMENT", "TotalAmt": 110, "TxnDate": "2026-10-05",
            "CustomerRef": {"value": "QBO-CUSTOMER"}, "CurrencyRef": {"value": self.company.currency_id.name},
            "DepositToAccountRef": {"value": "QBO-BANK"},
            "Line": [{"Amount": 100, "LinkedTxn": [{"TxnId": "16X9-SYNTHETIC-INVOICE", "TxnType": "Invoice"}]}]}
        self._engine()._upsert_payments([source])
        payment = self.env["account.payment"].search([("qbo_id", "=", source["Id"]), ("company_id", "=", self.company.id)])
        self.assertTrue(payment)
        self.assertTrue(payment.qbo_link_evidence)
        self.assertEqual(payment.qbo_link_state, "review")
        self.assertFalse(payment.qbo_linked_move_ids)

    def test_manager_rpc_persists_reviewed_mapping_and_returns_safe_data(self):
        self.env.user.company_ids = [fields.Command.link(self.company.id)]
        mapping = self._tax_map()
        result = self.env["qbo.tax.mapping"].apply_configuration(self.company.id,
            [{"source_tax_code": "REVIEWED", "direction": "sale", "tax_ids": mapping.tax_ids.ids, "active": True}], True)
        self.assertEqual(result["company_id"], self.company.id)
        self.assertTrue(any(row["source_tax_code"] == "REVIEWED" for row in result["mappings"]))
        self.assertNotIn("client_secret", result)
        self.assertNotIn("refresh_token", result)

    def test_posted_source_record_is_not_modified_on_repeat_import(self):
        self._tax_map(code="ZERO", taxes=self.env["account.tax"])
        source = self._source(code="ZERO", total=100)
        source["TxnTaxDetail"]["TotalTax"] = 0
        self._engine()._upsert_invoices([source])
        move = self._move()
        move.action_post()
        source["TotalAmt"] = 200
        source["Line"][0]["Amount"] = 200
        self._engine()._upsert_invoices([source])
        self.assertEqual(move.state, "posted")
        self.assertEqual(move.amount_total, 100)

    def test_changed_tax_rate_cannot_reuse_stale_native_totals(self):
        mapping = self._tax_map()
        self._engine()._upsert_invoices([self._source()])
        mapping.tax_ids.amount = 20
        self._move()._qbo_validate_conversion()
        self.assertEqual(self._move().qbo_conversion_state, "review")

    def test_read_projection_detects_stale_tax_validation_without_writing(self):
        self.env.user.company_ids = [fields.Command.link(self.company.id)]
        mapping = self._tax_map()
        self._engine()._upsert_invoices([self._source()])
        mapping.tax_ids.amount = 20
        result = self.env["qbo.tax.mapping"].review_configuration(self.company.id)
        self.assertTrue(any(row["source_id"] == "16X9-SYNTHETIC-INVOICE" for row in result["exceptions"]))
        self.assertEqual(self._move().qbo_conversion_state, "validated")

    def test_foreign_journal_currency_is_not_validated_as_company_currency(self):
        self._tax_map()
        self._engine()._upsert_invoices([self._source()])
        currencies = self.env["res.currency"].with_context(active_test=False).search([("id", "!=", self.company.currency_id.id)], limit=1)
        self.assertTrue(currencies)
        currencies.active = True
        self.bank_account.currency_id = currencies
        journal = self.env["account.journal"].search([("company_id", "=", self.company.id), ("type", "=", "bank")], limit=1)
        journal.currency_id = currencies
        journal.default_account_id = self.bank_account
        source = {"Id": "16X9-FOREIGN-JOURNAL-PAYMENT", "TotalAmt": 110, "TxnDate": "2026-10-05",
            "CustomerRef": {"value": "QBO-CUSTOMER"}, "CurrencyRef": {"value": self.company.currency_id.name},
            "DepositToAccountRef": {"value": "QBO-BANK"},
            "Line": [{"Amount": 100, "LinkedTxn": [{"TxnId": "16X9-SYNTHETIC-INVOICE", "TxnType": "Invoice"}]}]}
        self._engine()._upsert_payments([source])
        payment = self.env["account.payment"].search([("qbo_id", "=", source["Id"]), ("company_id", "=", self.company.id)])
        self.assertTrue(payment)
        self.assertEqual(payment.qbo_link_state, "review")

    def test_explicit_unmapped_bank_never_falls_back_to_an_arbitrary_journal(self):
        source = {"Id": "16X9-UNMAPPED-BANK-PAYMENT", "TotalAmt": 110, "TxnDate": "2026-10-05",
            "CustomerRef": {"value": "QBO-CUSTOMER"}, "CurrencyRef": {"value": self.company.currency_id.name},
            "DepositToAccountRef": {"value": "UNMAPPED-BANK"}}
        self._engine()._upsert_payments([source])
        self.assertFalse(self.env["account.payment"].search([("qbo_id", "=", source["Id"]), ("company_id", "=", self.company.id)]))

    def test_us_taxable_marker_uses_explicit_document_tax_code(self):
        self._tax_map(code="DOC-TAX")
        source = self._source(code="TAX")
        source["TxnTaxDetail"]["TxnTaxCodeRef"] = {"value": "DOC-TAX"}
        self._engine()._upsert_invoices([source])
        self.assertEqual(self._move().amount_tax, 10)
        self.assertEqual(self._move().qbo_conversion_state, "validated")

    def test_non_taxable_line_does_not_inherit_document_tax_code(self):
        self._tax_map(code="DOC-TAX")
        self._tax_map(code="NON", taxes=self.env["account.tax"])
        source = self._source(code="NON", total=100)
        source["TxnTaxDetail"] = {"TotalTax": 0, "TxnTaxCodeRef": {"value": "DOC-TAX"}}
        self._engine()._upsert_invoices([source])
        self.assertEqual(self._move().amount_tax, 0)
        self.assertEqual(self._move().qbo_conversion_state, "validated")
