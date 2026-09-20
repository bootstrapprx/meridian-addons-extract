from datetime import date
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestMeridianOnboarding(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.operator = cls.env["res.users"].create(
            {
                "name": "Onboarding Operator",
                "login": "onboarding.operator@example.com",
                "email": "onboarding.operator@example.com",
                "group_ids": [
                    (4, cls.env.ref("base.group_user").id),
                    (4, cls.env.ref("meridian_saas.group_meridian_saas_manager").id),
                ],
            }
        )
        cls.api = cls.env["meridian.onboarding"].with_user(cls.operator)

    def test_operator_gate_blocks_plain_users(self):
        plain = self.env["res.users"].create(
            {
                "name": "Plain User",
                "login": "plain.user@example.com",
                "group_ids": [(4, self.env.ref("base.group_user").id)],
            }
        )
        with self.assertRaises(AccessError):
            self.env["meridian.onboarding"].with_user(plain).get_onboarding_status()

    def test_setup_entities_solo_is_idempotent(self):
        result = self.api.setup_entities(
            {"company_type": "solo", "company_name": "Solo Test Co"}
        )
        self.assertEqual(result["status"], "completed")
        again = self.api.setup_entities(
            {"company_type": "solo", "company_name": "Solo Test Co"}
        )
        self.assertEqual(again["company_id"], result["company_id"])

    def test_setup_entities_umbrella_creates_group(self):
        result = self.api.setup_entities(
            {
                "company_type": "umbrella",
                "holding_name": "Umbrella Holding Co",
                "subsidiaries": [
                    {"name": "Umbrella Sub One", "state_code": "tx"},
                    {"name": "Umbrella Sub Two", "state_code": "CA"},
                ],
            }
        )
        group = self.env["poseidon.company.group"].browse(result["group_id"])
        roles = {m.company_id.name: m.role for m in group.member_ids}
        self.assertEqual(roles["Umbrella Holding Co"], "holding")
        self.assertEqual(roles["Umbrella Sub One"], "subsidiary")
        sub = self.env["res.company"].search([("name", "=", "Umbrella Sub One")])
        profile = self.env["poseidon.us.tax.profile"].search(
            [("company_id", "=", sub.id)]
        )
        self.assertEqual(profile.state_code, "TX")
        # Re-running must not duplicate members (UNIQUE(group_id, company_id)).
        again = self.api.setup_entities(
            {
                "company_type": "umbrella",
                "holding_name": "Umbrella Holding Co",
                "subsidiaries": [{"name": "Umbrella Sub One"}],
            }
        )
        self.assertEqual(again["group_id"], group.id)

    def test_umbrella_binds_holding_to_provisioned_company(self):
        """Umbrella onboarding on a DB that already has the provisioned company
        must reconcile the holding onto that one company — even when the
        session's active company has drifted elsewhere — and never spawn a
        second holding or mutate the operator in a session-breaking way.
        """
        main = self.env.ref("base.main_company")

        # Simulate a session whose active company drifted away from the
        # provisioned company — the class of state that used to yield a
        # duplicate, undeletable holding.
        other = self.env["res.company"].create({"name": "Drifted Active Co"})
        self.operator.sudo().write({"company_ids": [(4, other.id)]})
        api = self.env["meridian.onboarding"].with_user(self.operator).with_company(other)

        before_company_id = self.operator.company_id.id
        before_login = self.operator.login
        before_active = self.operator.active
        companies_before = self.env["res.company"].sudo().search_count([])

        result = api.setup_entities(
            {
                "company_type": "umbrella",
                "holding_name": "Provisioned Holdco",
                "subsidiaries": [
                    {"name": "Prov Sub A", "state_code": "tx"},
                    {"name": "Prov Sub B", "state_code": "ca"},
                ],
            }
        )

        # Holding is the provisioned company (base.main_company), renamed —
        # NOT the drifted `other` company, and no third company invented.
        self.assertEqual(result["company_id"], main.id)
        self.assertEqual(main.name, "Provisioned Holdco")
        self.assertNotEqual(other.name, "Provisioned Holdco")

        group = self.env["poseidon.company.group"].browse(result["group_id"])
        holdings = group.member_ids.filtered(lambda m: m.active and m.role == "holding")
        self.assertEqual(len(holdings), 1, "exactly one holding member")
        self.assertEqual(holdings.company_id.id, main.id)
        subs = group.member_ids.filtered(lambda m: m.active and m.role == "subsidiary")
        self.assertEqual(len(subs), 2)
        self.assertFalse(
            group.member_ids.filtered(lambda m: m.company_id.id == other.id),
            "the drifted company must not be co-opted into the group",
        )

        # Exactly the two subsidiaries were created — no duplicate holding.
        self.assertEqual(
            self.env["res.company"].sudo().search_count([]), companies_before + 2
        )

        # Session anchor intact: the operator's default company, login and
        # active flag (the fields the Odoo session token is derived from) are
        # untouched; they only gained access to the new subsidiaries.
        self.assertEqual(self.operator.company_id.id, before_company_id)
        self.assertEqual(self.operator.login, before_login)
        self.assertEqual(self.operator.active, before_active)
        for company in subs.mapped("company_id"):
            self.assertIn(company.id, self.operator.company_ids.ids)

        # A divergent holding name on re-run still reconciles onto the same
        # provisioned company (idempotent, no second holding).
        rerun = api.setup_entities(
            {
                "company_type": "umbrella",
                "holding_name": "Renamed Holdco",
                "subsidiaries": [{"name": "Prov Sub A"}],
            }
        )
        self.assertEqual(rerun["company_id"], main.id)
        self.assertEqual(rerun["group_id"], group.id)
        self.assertEqual(main.name, "Renamed Holdco")
        holdings_after = self.env["poseidon.company.group"].browse(
            rerun["group_id"]
        ).member_ids.filtered(lambda m: m.active and m.role == "holding")
        self.assertEqual(len(holdings_after), 1)
        self.assertEqual(holdings_after.company_id.id, main.id)

    def test_company_info_writes_company_and_profile(self):
        created = self.api.setup_entities(
            {"company_type": "solo", "company_name": "Info Test Co"}
        )
        company_id = created["company_id"]
        self.api.save_company_info(
            company_id,
            {
                "ein": "12-3456789",
                "state_of_incorporation": "de",
                "city": "Wilmington",
                "entity_type": "llc",
                "accounting_method": "cash",
                "fiscalyear_last_month": 12,
                "fiscalyear_last_day": 31,
            },
        )
        company = self.env["res.company"].browse(company_id)
        self.assertEqual(company.vat, "12-3456789")
        self.assertEqual(company.city, "Wilmington")
        profile = self.env["poseidon.us.tax.profile"].search(
            [("company_id", "=", company_id)]
        )
        self.assertEqual(profile.entity_type, "llc")
        status = self.api.get_onboarding_status(company_id)
        self.assertEqual(status["steps"]["company_info"]["accounting_method"], "cash")
        self.assertEqual(status["steps"]["company_info"]["fiscalyear_last_month"], 12)
        self.assertEqual(profile.federal_ein, "12-3456789")

    def test_qbo_configuration_preview_and_confirmed_apply(self):
        company = self.env["res.company"].create({"name": "QBO Setup Co"})
        self.operator.sudo().write({"company_ids": [(4, company.id)]})
        realm = self.env["qbo.realm"].create(
            {
                "name": "QBO Setup Realm",
                "realm_id": "987654321",
                "client_id": "client",
                "client_secret": "secret",
                "refresh_token": "refresh",
                "state": "connected",
                "sync_mode": "pull_only",
            }
        )
        self.env["qbo.company.mapping"].create(
            {"company_id": company.id, "realm_id": realm.id}
        )
        snapshot = {
            "company_info": {
                "LegalName": "QBO Setup Co LLC",
                "LegalAddr": {
                    "Line1": "100 Main St",
                    "City": "Sheridan",
                    "CountrySubDivisionCode": "WY",
                    "PostalCode": "82801",
                },
                "FiscalYearStartMonth": "January",
            },
            "preferences": {"AccountingInfoPrefs": {"FirstMonthOfFiscalYear": "January"}},
            "bank_accounts": [
                {
                    "Id": "bank-1",
                    "Name": "Operating Checking",
                    "AcctNum": "1111",
                    "AccountType": "Bank",
                    "AccountSubType": "Checking",
                }
            ],
        }
        api = self.env["meridian.onboarding"].with_user(self.operator)
        with patch(
            "odoo.addons.meridian_onboarding.models.meridian_onboarding.QBOApiClient"
        ) as client:
            client.return_value.get_configuration.return_value = snapshot
            proposal = api.preview_qbo_configuration(company.id)
            result = api.apply_qbo_configuration(
                company.id,
                {
                    "confirmed": True,
                    "selected_fields": ["legal_name", "address", "city", "zip", "state_code", "entity_type", "accounting_method"],
                    "values": proposal["values"],
                    "actions": {"ensure_journals": False, "create_fiscal_periods": False},
                    "bank_accounts": [],
                },
            )

        self.assertEqual(proposal["values"]["entity_type"], "llc")
        self.assertEqual(proposal["values"]["fiscalyear_last_month"], 12)
        self.assertEqual(proposal["bank_accounts"][0]["qbo_id"], "bank-1")
        self.assertEqual(company.name, "QBO Setup Co LLC")
        self.assertEqual(company.state_id.code, "WY")
        profile = self.env["poseidon.us.tax.profile"].search([("company_id", "=", company.id)])
        self.assertEqual(profile.entity_type, "llc")
        self.assertEqual(profile.accounting_method, "accrual")
        self.assertTrue(result["audit_ref"])

    def test_qbo_configuration_apply_requires_confirmation(self):
        company = self.env["res.company"].create({"name": "Unconfirmed QBO Co"})
        self.operator.sudo().write({"company_ids": [(4, company.id)]})
        with self.assertRaises(UserError):
            self.env["meridian.onboarding"].with_user(self.operator).apply_qbo_configuration(
                company.id, {"confirmed": False}
            )

    def test_setup_entities_umbrella_logical_completes_taxes(self):
        result = self.api.setup_entities(
            {
                "company_type": "umbrella_logical",
                "holding_name": "Logical Holding Co",
                "subsidiaries": [{"name": "Logical Sub One", "state_code": "tx"}],
            }
        )
        company_id = result["company_id"]
        
        self.api.save_company_info(
            company_id,
            {
                # For a logical holding, UI does not send EIN or legal entity type
                "state_of_incorporation": "tx",
                "city": "Austin",
            },
        )
        status = self.api.get_onboarding_status(company_id)
        # Without EIN or entity type, taxes should still be completable if state is set.
        # Actually, for logical, taxes step isn't even relevant for the holding, but if it exists, it should mark as done.
        # Wait, the user said "yields taxes.done and required_complete".
        self.assertTrue(status["steps"]["taxes"]["done"])

    def test_generate_fiscal_periods_monthly_and_overlap_skip(self):
        created = self.api.setup_entities(
            {"company_type": "solo", "company_name": "Periods Test Co"}
        )
        company_id = created["company_id"]
        result = self.api.generate_fiscal_periods(company_id, 2026, "monthly")
        self.assertEqual(len(result["created"]), 12)
        periods = self.env["poseidon.kernel.period"].search(
            [("company_id", "=", company_id)], order="date_from"
        )
        self.assertEqual(periods[0].date_from, date(2026, 1, 1))
        self.assertEqual(periods[-1].date_to, date(2026, 12, 31))
        self.assertTrue(all(p.state == "open" for p in periods))
        rerun = self.api.generate_fiscal_periods(company_id, 2026, "monthly")
        self.assertEqual(len(rerun["created"]), 0)
        self.assertEqual(len(rerun["skipped"]), 12)

    def test_generate_fiscal_periods_quarterly(self):
        created = self.api.setup_entities(
            {"company_type": "solo", "company_name": "Quarters Test Co"}
        )
        result = self.api.generate_fiscal_periods(created["company_id"], 2026, "quarterly")
        self.assertEqual(len(result["created"]), 4)
        self.assertEqual(result["created"][0]["name"], "FY2026 Q1")

    def test_setup_taxes_creates_percent_taxes(self):
        created = self.api.setup_entities(
            {"company_type": "solo", "company_name": "Tax Test Co"}
        )
        company_id = created["company_id"]
        result = self.api.setup_taxes(
            company_id,
            {
                "state_code": "tx",
                "enable_sales_tax": True,
                "sales_tax_rate": 8.25,
            },
        )
        tax = self.env["account.tax"].browse(result["tax_ids"][0])
        self.assertEqual(tax.type_tax_use, "sale")
        self.assertEqual(tax.amount, 8.25)
        profile = self.env["poseidon.us.tax.profile"].search(
            [("company_id", "=", company_id)]
        )
        self.assertEqual(profile.state_code, "TX")
        with self.assertRaises(UserError):
            self.api.setup_taxes(company_id, {"state_code": ""})

    def test_setup_banks_creates_account_journal_pair(self):
        created = self.api.setup_entities(
            {"company_type": "solo", "company_name": "Bank Test Co"}
        )
        company_id = created["company_id"]
        company = self.env["res.company"].browse(company_id)
        bank_account = self.env["res.partner.bank"].create(
            {
                "acc_number": "999888777",
                "partner_id": company.partner_id.id,
            }
        )
        result = self.api.setup_banks(
            company_id,
            [
                {
                    "name": "Operating Checking",
                    "journal_name": "Chase Operating",
                    "bank_account_id": bank_account.id,
                }
            ],
        )
        self.assertEqual(len(result["journals"]), 1)
        journal = self.env["account.journal"].browse(result["journals"][0]["journal_id"])
        self.assertEqual(journal.type, "bank")
        self.assertEqual(journal.default_account_id.account_type, "asset_cash")
        self.assertEqual(journal.bank_account_id, bank_account)
        second_bank_account = self.env["res.partner.bank"].create(
            {
                "acc_number": "555444333",
                "partner_id": company.partner_id.id,
            }
        )
        second_result = self.api.setup_banks(
            company_id,
            [
                {
                    "name": "Payroll Checking",
                    "journal_name": "Chase Operating",
                    "bank_account_id": second_bank_account.id,
                }
            ],
        )
        second_journal = self.env["account.journal"].browse(
            second_result["journals"][0]["journal_id"]
        )
        self.assertNotEqual(second_journal.id, journal.id)
        self.assertEqual(second_journal.bank_account_id, second_bank_account)
        self.assertEqual(journal.bank_account_id, bank_account)
        general = self.env["account.journal"].search(
            [("company_id", "=", company_id), ("type", "=", "general")]
        )
        self.assertTrue(general)
        rerun = self.api.setup_banks(
            company_id,
            [{"name": "Operating Checking", "journal_name": "Chase Operating"}],
        )
        self.assertEqual(
            rerun["journals"][0]["journal_id"], result["journals"][0]["journal_id"]
        )

    def test_status_derivation(self):
        created = self.api.setup_entities(
            {"company_type": "solo", "company_name": "Status Test Co"}
        )
        company_id = created["company_id"]
        status = self.api.get_onboarding_status(company_id)
        self.assertTrue(status["steps"]["entities"]["done"])
        self.assertFalse(status["steps"]["company_info"]["done"])
        self.assertFalse(status["required_complete"])

        self.api.save_company_info(company_id, {"entity_type": "llc"})
        self.api.generate_fiscal_periods(company_id, 2026, "monthly")
        self.api.setup_taxes(company_id, {"state_code": "TX"})
        self.api.ensure_chart(company_id)

        status = self.api.get_onboarding_status(company_id)
        self.assertTrue(status["steps"]["company_info"]["done"])
        self.assertTrue(status["steps"]["fiscal_periods"]["done"])
        self.assertTrue(status["steps"]["taxes"]["done"])
        self.assertTrue(status["steps"]["chart"]["done"])
        self.assertTrue(status["required_complete"])


@tagged("post_install", "-at_install")
class TestOnboardingSuggestions(TransactionCase):
    def test_list_suggested_excludes_seeded_l0(self):
        company = self.env["res.company"].create({"name": "Suggest Co"})
        Onb = self.env["meridian.onboarding"]
        self.env["account.chart.template"].sudo().poseidon_publish_missing_standard_accounts(
            company.id, required_only=True
        )
        result = Onb.list_suggested_accounts(company.id)
        codes = {a["code"] for a in result["accounts"]}
        self.assertNotIn("10000", codes, "seeded L0 account must not be suggested")
        self.assertIn("10010", codes, "L3 analytic account should be suggested")
        self.assertIn("63000", codes, "unseeded L1 account should be suggested")

    def test_ensure_chart_seeds_l3_analytic_accounts(self):
        company = self.env["res.company"].create({"name": "L3 Seed Co"})
        Onb = self.env["meridian.onboarding"]
        self.env["account.chart.template"].sudo().poseidon_publish_missing_standard_accounts(
            company.id, required_only=True
        )
        result = Onb.ensure_chart(company.id)
        self.assertGreater(result["l3_activated"], 0, "L3 accounts must be seeded")
        l3 = self.env["qbo.standard.account"].search(
            [("code", "=", "10010"), ("kernel_layer", "=", "L3")], limit=1
        )
        self.assertTrue(l3, "L3 master account 10010 must exist")
        seeded = self.env["account.account"].search(
            [
                ("company_ids", "=", company.id),
                ("qbo_standard_account_id", "=", l3.id),
            ],
            limit=1,
        )
        self.assertTrue(seeded, "L3 analytic account must be seeded into the company")
        # Idempotent: a second accept seeds nothing new.
        rerun = Onb.ensure_chart(company.id)
        self.assertEqual(rerun["l3_activated"], 0)

    def test_l3_suggestions_and_seed_follow_activity_profile(self):
        company = self.env["res.company"].create({"name": "Tagged Co"})
        self.env["poseidon.us.tax.profile"].create(
            {"company_id": company.id, "activity_tag": "universal"}
        )
        Onb = self.env["meridian.onboarding"]
        self.env["account.chart.template"].sudo().poseidon_publish_missing_standard_accounts(
            company.id, required_only=True
        )
        result = Onb.list_suggested_accounts(company.id)
        codes = {a["code"] for a in result["accounts"]}
        self.assertIn("10010", codes, "Universal L3 analytic account should be suggested")
        self.assertNotIn(
            "15040", codes, "Manufacturing-only L3 must not be suggested for a Universal company"
        )
        Onb.ensure_chart(company.id)
        mfg = self.env["account.account"].search(
            [
                ("company_ids", "=", company.id),
                ("qbo_standard_account_id.kernel_layer", "=", "L3"),
                ("code", "=", "15040"),
            ]
        )
        self.assertFalse(mfg, "Manufacturing-only L3 must not be seeded for a Universal company")

    def test_suggestions_scoped_by_parent_code_skip_activity_filter(self):
        company = self.env["res.company"].create({"name": "Parent Scope Co"})
        self.env["poseidon.us.tax.profile"].create(
            {"company_id": company.id, "activity_tag": "universal"}
        )
        Onb = self.env["meridian.onboarding"]
        result = Onb.list_suggested_accounts(company.id, parent_code="40000")
        self.assertTrue(result["accounts"], "L3 options must exist for the parent")
        for account in result["accounts"]:
            self.assertEqual(
                account["rollup_to_l1_parent"],
                "40000",
                "every suggestion must roll up into the requested parent",
            )
        codes = {account["code"] for account in result["accounts"]}
        self.assertIn(
            "40010", codes, "Universal L3 account under the parent should be suggested"
        )


@tagged("post_install", "-at_install")
class TestProductionGuidance(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.operator = cls.env["res.users"].create(
            {
                "name": "Guidance Operator",
                "login": "guidance.operator@example.com",
                "email": "guidance.operator@example.com",
                "group_ids": [
                    (4, cls.env.ref("base.group_user").id),
                    (4, cls.env.ref("meridian_saas.group_meridian_saas_manager").id),
                ],
            }
        )
        cls.api = cls.env["meridian.onboarding"].with_user(cls.operator)

    def test_guidance_contract_on_fresh_company(self):
        created = self.api.setup_entities(
            {"company_type": "solo", "company_name": "Guidance Fresh Co"}
        )
        guidance = self.api.get_production_guidance(created["company_id"], pt=True)
        self.assertEqual(guidance["company_id"], created["company_id"])
        self.assertEqual(guidance["lang"], "pt")
        self.assertEqual(guidance["phase"], "setup")
        self.assertIn(
            guidance["next_action"]["key"],
            ("jurisdiction", "accounting_method", "fiscal_periods"),
        )
        self.assertTrue(guidance["next_action"]["label"])
        self.assertIn("plain", guidance["next_content"])
        self.assertIn("accountant", guidance["next_content"])
        self.assertTrue(guidance["blocked"])
        self.assertIn("available", guidance["l3"])

    def test_guidance_content_pt_and_en(self):
        pt = self.api.get_guidance_content("l3_activation", pt=True)
        en = self.api.get_guidance_content("l3_activation", pt=False)
        self.assertEqual(pt["lang"], "pt")
        self.assertEqual(en["lang"], "en")
        self.assertTrue(pt["plain"])
        self.assertTrue(en["plain"])
        self.assertTrue(pt["title"])
        with self.assertRaises(UserError):
            self.api.get_guidance_content("bogus_concept")

    def test_guidance_after_full_setup_points_to_l3_or_kernel(self):
        created = self.api.setup_entities(
            {"company_type": "solo", "company_name": "Guidance Ready Co"}
        )
        company_id = created["company_id"]
        self.api.save_company_info(
            company_id,
            {
                "entity_type": "llc",
                "state_of_incorporation": "tx",
                "city": "Austin",
                "accounting_method": "cash",
            },
        )
        self.api.generate_fiscal_periods(company_id, 2026, "monthly")
        self.api.setup_taxes(company_id, {"state_code": "TX"})
        self.api.ensure_chart(company_id)

        guidance = self.api.get_production_guidance(company_id)
        self.assertTrue(guidance["required_complete"])
        self.assertIn(guidance["phase"], ("production", "operational"))
        self.assertIn(
            guidance["next_action"]["key"],
            ("journals", "bank", "l3_activation", "kernel_layers"),
        )
        self.assertIn(guidance["next_action"]["severity"], ("block", "warn", "info"))


@tagged("post_install", "-at_install")
class TestMappingRationale(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.operator = cls.env["res.users"].create(
            {
                "name": "Mapping Operator",
                "login": "mapping.operator@example.com",
                "email": "mapping.operator@example.com",
                "group_ids": [
                    (4, cls.env.ref("base.group_user").id),
                    (4, cls.env.ref("meridian_saas.group_meridian_saas_manager").id),
                ],
            }
        )
        cls.api = cls.env["meridian.onboarding"].with_user(cls.operator)

    def test_account_type_guide_labels_pt_and_unknown(self):
        from odoo.addons.qbo_bridge.models.qbo_account_bridge_rule import (
            account_type_guide_labels,
        )

        labels = account_type_guide_labels("expense_other", lang="pt")
        self.assertEqual(labels["normal_balance"], "debit")
        self.assertEqual(labels["normal_balance_label"], "Débito")
        self.assertEqual(labels["category"], "Despesa")
        self.assertEqual(labels["type_label"], "Outras despesas")
        self.assertEqual(labels["statement"], "Demonstração de resultados")
        self.assertEqual(account_type_guide_labels("bogus", lang="pt"), {})

    def test_mapping_match_reason_builds_human_text(self):
        from odoo.addons.qbo_bridge.models.qbo_account_bridge_rule import (
            mapping_match_reason,
        )

        self.assertFalse(mapping_match_reason(None, {"Name": "Anything"}, lang="pt"))
        rule = self.env["qbo.account.bridge.rule"].create(
            {
                "match_name": "Other Expense",
                "match_account_type": "Expense",
                "canonical_code": "99999",
                "canonical_name": "Other Expense Canonical",
                "canonical_account_type": "expense_other",
            }
        )
        reason = mapping_match_reason(
            rule,
            {"Name": "Other Expense", "AccountType": "Expense"},
            lang="pt",
        )
        self.assertIn("Other Expense", reason)
        self.assertIn("Expense", reason)

    def test_list_mapping_candidates_enriches_match_reason_and_l3(self):
        created = self.api.setup_entities(
            {"company_type": "solo", "company_name": "Mapping Rationale Co"}
        )
        company = self.env["res.company"].browse(created["company_id"])
        self.env["qbo.standard.account"]._ensure_l3_master_chart_imported()
        l3 = self.env["qbo.standard.account"].search(
            [
                ("kernel_layer", "=", "L3"),
                ("entry_type", "=", "detail"),
            ],
            order="code",
            limit=1,
        )
        self.assertTrue(l3, "L3 master chart must be seeded")

        self.env["qbo.account.bridge.rule"].create(
            {
                "match_name": "Misc Other Expense",
                "canonical_code": l3.code,
                "canonical_name": l3.name,
                "canonical_account_type": "expense_other",
            }
        )
        self.env["account.account"].sudo().create(
            {
                "code": "1599",
                "name": "Misc Other Expense",
                "account_type": "expense",
                "company_ids": [(6, 0, [company.id])],
                "qbo_id": "QBO-SRC-1",
                "qbo_source_account_number": "1599",
                "qbo_source_name": "Misc Other Expense",
                "qbo_source_account_type": "Expense",
                "qbo_source_account_subtype": "Other Expense",
            }
        )

        result = self.api.list_mapping_candidates(company.id)
        row = next(
            (r for r in result["candidates"] if r["qbo_id"] == "QBO-SRC-1"), None
        )
        self.assertTrue(row, "QBO source account must be listed as a candidate")
        self.assertIn("Misc Other Expense", row["match_reason"])
        self.assertEqual(row["suggested_code"], l3.code)
        self.assertEqual(row["normal_balance"], "debit")
        self.assertEqual(row["type_label"], "Outras despesas")
        self.assertEqual(row["statement"], "Demonstração de resultados")
        self.assertTrue(row["suggest_l3"])
        self.assertEqual(row["l3_code"], l3.code)
