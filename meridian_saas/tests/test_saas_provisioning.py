from unittest.mock import patch
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, tagged

@tagged("post_install", "-at_install", "meridian_saas")
class TestMeridianSaasProvisioning(TransactionCase):

    def setUp(self):
        super().setUp()
        self.saas_model = self.env["meridian.saas"]
        self.request_model = self.env["meridian.saas.request"]
        self.event_model = self.env["meridian.saas.event"]
        self.tenant_db_model = self.env["meridian.saas.tenant_db"]
        self.internal_user = self.env["res.users"].create({
            "name": "Selfserve Tester",
            "login": "selfserve_tester@test.com",
            "group_ids": [(6, 0, [
                self.env.ref("meridian_saas.group_meridian_saas_manager").id,
            ])],
        })

    def _assert_kernel_seeded(self, company):
        Account = self.env["account.account"].sudo().with_context(active_test=False)
        seeded = Account.search(
            [("company_ids", "=", company.id), ("qbo_standard_account_id", "!=", False)]
        )
        self.assertTrue(seeded, "company should have kernel accounts seeded at creation")
        self.assertTrue(
            all(a.poseidon_master_account_id for a in seeded),
            "every seeded account must carry a kernel master id",
        )
        expected = self.env["qbo.standard.account"].sudo().search(
            [("entry_type", "=", "detail"), ("kernel_required", "=", True)]
        )
        self.assertEqual(
            set(seeded.with_company(company).mapped("code")),
            set(expected.mapped("code")),
            "creation must seed exactly the L0 (kernel_required) subset",
        )

    def test_idempotency_returns_cached_result(self):
        req_id = "test_req_123"
        tenant_id = "test_tenant_123"
        slug = "acme-co"
        
        req_row = self.request_model.create({
            "request_id": req_id,
            "tenant_id": tenant_id,
            "product_key": "meridian",
            "state": "completed",
            "odoo_db": slug,
            "odoo_company_id": 1,
            "odoo_partner_id": 1,
            "odoo_user_id": 1,
        })

        payload = {
            "request_id": req_id,
            "tenant_id": tenant_id,
            "tenant_slug": slug,
            "product_key": "meridian",
        }
        res1 = self.saas_model.provision_usgaap_kit(payload)
        self.assertEqual(res1["status"], "completed")
        self.assertEqual(res1["odoo_db"], slug)

    def test_provision_workspace_records(self):
        req_id = "test_req_complete"
        email = "owner@complete.com"
        company_name = "Acme Complete LLC"

        payload = {
            "request_id": req_id,
            "display_name": "Acme Complete",
            "owner_email": email,
            "company_name": company_name,
            "plan_code": "pro",
            "entitlements": ["meridian.usgaap"],
        }

        # Assert against _provision_workspace_records directly since DB creation cannot happen in TransactionCase
        company, partner, user = self.saas_model._provision_workspace_records(self.env, payload, req_id)

        self.assertTrue(company.exists())
        self.assertEqual(company.name, company_name)
        self.assertEqual(company.chart_template, "poseidon_qbo_us")
        self._assert_kernel_seeded(company)

        self.assertTrue(partner.exists())
        self.assertEqual(partner.email, email)

        self.assertTrue(user.exists())
        self.assertEqual(user.login, email)
        self.assertIn(company.id, user.company_ids.ids)

        group_acc_manager = self.env.ref("meridian_saas.group_meridian_saas_manager")
        self.assertIn(user.id, group_acc_manager.user_ids.ids)
        self.assertNotIn(user.id, self.env.ref("base.group_system").user_ids.ids)

    def test_pending_fresh_claim_returns_pending(self):
        req_id = "test_req_pending"
        tenant_id = "test_tenant_pending"
        slug = "acme-pending"
        
        self.request_model.create({
            "request_id": req_id,
            "tenant_id": tenant_id,
            "product_key": "meridian",
            "state": "pending",
        })
        
        # Manually claim it
        self.env.cr.execute("UPDATE meridian_saas_request SET claimed_at = now() AT TIME ZONE 'UTC' WHERE request_id = %s", (req_id,))

        payload = {
            "request_id": req_id,
            "tenant_id": tenant_id,
            "tenant_slug": slug,
        }
        res = self.saas_model.provision_usgaap_kit(payload)
        self.assertEqual(res["status"], "pending")

    def test_invalid_slug_returns_failed(self):
        payload = {
            "request_id": "req",
            "tenant_id": "tenant",
            "tenant_slug": "Invalid-Slug",
        }
        res = self.saas_model.provision_usgaap_kit(payload)
        self.assertEqual(res["status"], "failed")
        self.assertFalse(self.tenant_db_model.search([("db_name", "=", "Invalid-Slug")]))

        payload["tenant_slug"] = "meridian"
        res = self.saas_model.provision_usgaap_kit(payload)
        self.assertEqual(res["status"], "failed")
        self.assertFalse(self.tenant_db_model.search([("db_name", "=", "meridian")]))

    def test_create_owned_company_configures_and_grants(self):
        user = self.internal_user
        before_ids = set(user.company_ids.ids)
        res = self.saas_model.with_user(user).create_owned_company("Self Serve Co")
        self.assertEqual(res["status"], "completed")
        company = self.env["res.company"].browse(res["company_id"])
        self.assertTrue(company.exists())
        self.assertEqual(company.name, "Self Serve Co")
        self.assertEqual(company.chart_template, "poseidon_qbo_us")
        self._assert_kernel_seeded(company)
        self.assertIn(company.id, user.company_ids.ids)
        self.assertNotIn(company.id, before_ids)
        group = self.env.ref("meridian_saas.group_meridian_saas_manager")
        self.assertIn(user.id, group.user_ids.ids)

    def test_create_owned_company_rejects_blank(self):
        res = self.saas_model.with_user(self.internal_user).create_owned_company("   ")
        self.assertEqual(res["status"], "invalid")

    def test_create_owned_company_rejects_duplicate(self):
        self.saas_model.with_user(self.internal_user).create_owned_company("Dup Co")
        res = self.saas_model.with_user(self.internal_user).create_owned_company("Dup Co")
        self.assertEqual(res["status"], "duplicate")

    def test_create_owned_company_never_reuses_another_users_company(self):
        Users = self.env["res.users"]
        groups = [
            self.env.ref("base.group_user").id,
            self.env.ref("meridian_saas.group_meridian_saas_manager").id,
        ]
        internal = [(6, 0, groups)]
        user_a = Users.create({"name": "A", "login": "a_selfserve@test.com", "group_ids": internal})
        user_b = Users.create({"name": "B", "login": "b_selfserve@test.com", "group_ids": internal})
        res_a = self.saas_model.with_user(user_a).create_owned_company("Shared Name Co")
        self.assertEqual(res_a["status"], "completed")
        res_b = self.saas_model.with_user(user_b).create_owned_company("Shared Name Co")
        self.assertEqual(res_b["status"], "duplicate")
        self.assertNotIn(res_a["company_id"], user_b.company_ids.ids)

    def test_create_owned_company_rejects_non_internal_user(self):
        portal = self.env["res.users"].create({
            "name": "Portal",
            "login": "portal_selfserve@test.com",
            "group_ids": [(6, 0, [self.env.ref("base.group_portal").id])],
        })
        with self.assertRaises(AccessError):
            self.saas_model.with_user(portal).create_owned_company("Nope Co")

    def test_create_owned_company_rejects_member_roles(self):
        for xmlid, login in [
            ("meridian_saas.group_meridian_saas_viewer", "viewer_selfserve@test.com"),
            ("meridian_saas.group_meridian_saas_member", "member_selfserve@test.com"),
        ]:
            user = self.env["res.users"].create({
                "name": login,
                "login": login,
                "group_ids": [(6, 0, [self.env.ref(xmlid).id])],
            })
            with self.assertRaises(AccessError):
                self.saas_model.with_user(user).create_owned_company("Role Gate Co")

    def test_create_owned_company_allows_accountant_role(self):
        user = self.env["res.users"].create({
            "name": "Accountant Selfserve",
            "login": "accountant_selfserve@test.com",
            "group_ids": [(6, 0, [
                self.env.ref("meridian_saas.group_meridian_saas_accountant").id,
            ])],
        })
        res = self.saas_model.with_user(user).create_owned_company("Accountant Co")
        self.assertEqual(res["status"], "completed")


@tagged("post_install", "-at_install", "meridian_saas")
class TestForeignChartPurge(TransactionCase):
    """Odoo auto-loads a fallback chart onto a company that has no
    chart_template. Those accounts are postable but carry no kernel link, so
    they must be gone before a tenant is handed over — and deleted, not
    archived, because an archived account still reserves its code.
    """

    def setUp(self):
        super().setUp()
        self.company = self.env["res.company"].create({"name": "Purge Test Co"})
        self.env["meridian.saas"]._configure_company(self.company)
        self.Account = self.env["account.account"].sudo().with_company(self.company)

    def _foreign_account(self, code="909090"):
        return self.Account.create({
            "name": "Fallback Chart Account",
            "code": code,
            "account_type": "expense",
            "company_ids": [(4, self.company.id)],
        })

    def _kernel_codes(self):
        return set(
            self.Account.search([
                ("company_ids", "=", self.company.id),
                ("qbo_standard_account_id", "!=", False),
            ]).mapped("code")
        )

    def test_purge_deletes_foreign_and_repoints_plumbing(self):
        foreign = self._foreign_account()
        kernel_before = self._kernel_codes()
        self.assertTrue(kernel_before, "kernel chart must be seeded for this test to mean anything")

        journal = self.env["account.journal"].sudo().create({
            "name": "Fallback Sales", "code": "FBS", "type": "sale",
            "company_id": self.company.id, "default_account_id": foreign.id,
        })
        self.company.sudo().write({"income_account_id": foreign.id})

        result = self.env["meridian.saas"]._purge_foreign_chart(self.company)

        self.assertEqual(result["deleted"], 1)
        self.assertFalse(foreign.exists(), "foreign account must be deleted, not archived")
        self.assertEqual(self._kernel_codes(), kernel_before, "kernel accounts must be untouched")
        # Plumbing must land on a real kernel account, never on nothing.
        # account.code is company-dependent (code_store), so it only resolves
        # under the owning company's context.
        self.assertIn(
            journal.default_account_id.with_company(self.company).code, kernel_before
        )
        self.assertIn(
            self.company.income_account_id.with_company(self.company).code, kernel_before
        )
        # The code is genuinely free again — an archived account would still
        # reserve it and raise a duplicate-code ValidationError here.
        self.assertTrue(self._foreign_account())

    def _tax(self, name, template=False):
        """A tax on the test company; `template` gives it the xmlid a chart
        template load would leave behind. The name is derived from the test
        company id because the DB's own company 1 already owns
        account.1_sale_tax_template from its generic_coa load.
        """
        country = self.env.ref("base.us")
        group = self.env["account.tax.group"].sudo().search(
            [("company_id", "=", self.company.id)], limit=1
        ) or self.env["account.tax.group"].sudo().create(
            {"name": "Purge Taxes", "company_id": self.company.id, "country_id": country.id}
        )
        tax = self.env["account.tax"].sudo().create({
            "name": name, "amount_type": "percent", "amount": 7.0, "type_tax_use": "sale",
            "company_id": self.company.id, "country_id": country.id, "tax_group_id": group.id,
        })
        if template:
            self.env["ir.model.data"].sudo().create({
                "module": "account", "name": f"{self.company.id}_sale_tax_template",
                "model": "account.tax", "res_id": tax.id,
            })
        return tax

    def test_purge_drops_template_taxes_and_keeps_operator_taxes(self):
        """Origin decides, not shape: a repair must not destroy real config."""
        self._foreign_account()
        template_tax = self._tax("Fallback 7%", template=True)
        operator_tax = self._tax("Operator Sales Tax")

        self.env["meridian.saas"]._purge_foreign_chart(self.company)

        self.assertFalse(template_tax.exists(), "template-created tax must be purged")
        self.assertTrue(
            operator_tax.exists(),
            "a tax the operator created has no xmlid and must survive the purge",
        )

    def test_purge_refuses_when_company_has_posted_lines(self):
        foreign = self._foreign_account()
        journal = self.env["account.journal"].sudo().create({
            "name": "Purge Guard", "code": "PGJ", "type": "general",
            "company_id": self.company.id,
        })
        other = self.Account.search([
            ("company_ids", "=", self.company.id), ("qbo_standard_account_id", "!=", False),
        ], limit=1)
        move = self.env["account.move"].sudo().with_company(self.company).create({
            "journal_id": journal.id,
            "line_ids": [
                (0, 0, {"account_id": foreign.id, "debit": 10.0, "credit": 0.0}),
                (0, 0, {"account_id": other.id, "debit": 0.0, "credit": 10.0}),
            ],
        })
        move.action_post()

        result = self.env["meridian.saas"]._purge_foreign_chart(self.company)

        self.assertEqual(result["skipped"], "posted_lines_exist")
        self.assertEqual(result["deleted"], 0)
        self.assertTrue(foreign.exists(), "must never destroy accounts that carry real entries")
