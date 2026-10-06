from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import new_test_user
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestMeridianKnowledge(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.a = cls.env.company
        cls.b = cls.env["res.company"].create({"name": "Knowledge second company"})
        cls.manager = new_test_user(
            cls.env,
            login="knowledge_manager",
            groups="document_page.group_document_manager,account.group_account_manager",
            company_id=cls.a.id,
            company_ids=[(6, 0, [cls.a.id, cls.b.id])],
        )
        cls.editor = new_test_user(
            cls.env,
            login="knowledge_editor",
            groups="document_page.group_document_editor,account.group_account_user",
            company_id=cls.a.id,
            company_ids=[(6, 0, [cls.a.id])],
        )
        cls.reader_b = new_test_user(
            cls.env,
            login="knowledge_reader_b",
            groups="document_knowledge.group_document_user",
            company_id=cls.b.id,
            company_ids=[(6, 0, [cls.b.id])],
        )
        cls.account_a = cls.env["account.account"].create(
            {
                "name": "Knowledge expense",
                "code": "998801",
                "poseidon_kernel_layer": "L3",
                "account_type": "expense",
                "company_ids": [(6, 0, [cls.a.id])],
            },
        )
        cls.account_b = cls.env["account.account"].create(
            {
                "name": "Knowledge foreign expense",
                "code": "998802",
                "poseidon_kernel_layer": "L3",
                "account_type": "expense",
                "company_ids": [(6, 0, [cls.b.id])],
            },
        )

    def values(self, **extra):
        return {
            "name": "Expense standard",
            "content": "<p>Reviewed evidence required.</p>",
            "content_format": "odoo",
            "scope": "company",
            "kind": "accounting",
            "account_ids": [self.account_a.id],
            **extra,
        }

    def create_page(self, **extra):
        return (
            self.env["document.page"]
            .with_user(self.manager)
            .meridian_knowledge_apply(self.a.id, "create", self.values(**extra))["page"]
        )

    def test_real_account_links_and_revision_history(self):
        page = self.create_page()
        result = (
            self.env["document.page"]
            .with_user(self.editor)
            .meridian_knowledge_snapshot(
                self.a.id, page["id"], account_id=self.account_a.id,
            )
        )
        self.assertEqual(result["page"]["account_ids"], [self.account_a.id])
        self.assertTrue(result["history"])
        self.assertFalse(
            self.env["account.move.line"].search_count(
                [("account_id", "=", self.account_a.id)],
            ),
        )

    def test_company_isolation_and_explicit_group_sharing(self):
        page = self.create_page()
        with self.assertRaises(AccessError):
            self.env["document.page"].with_user(
                self.reader_b,
            ).meridian_knowledge_snapshot(self.b.id, page["id"])
        shared = self.create_page(scope="group", company_ids=[self.a.id, self.b.id])
        result = (
            self.env["document.page"]
            .with_user(self.reader_b)
            .meridian_knowledge_snapshot(self.b.id, shared["id"])
        )
        self.assertEqual(result["page"]["scope"], "group")
        self.assertTrue(result["history"])

    def test_workspace_scope_requires_manager_even_for_direct_orm(self):
        page = self.create_page(scope="workspace", account_ids=[])
        read = (
            self.env["document.page"]
            .with_user(self.reader_b)
            .meridian_knowledge_snapshot(self.b.id, page["id"])
        )
        self.assertEqual(read["page"]["scope"], "workspace")
        with self.assertRaises(AccessError):
            self.env["document.page"].with_user(self.editor).meridian_knowledge_apply(
                self.a.id, "create", self.values(scope="workspace"),
            )
        with self.assertRaises(AccessError):
            self.env["document.page"].with_user(self.editor).browse(page["id"]).write(
                {"name": "Unauthorized edit"},
            )

    def test_foreign_account_link_rejected(self):
        with self.assertRaises(AccessError), self.cr.savepoint():
            self.create_page(account_ids=[self.account_b.id])

    def test_review_publication_and_published_edit_protection(self):
        page = self.create_page()
        service = self.env["document.page"].with_user(self.manager)
        self.assertFalse(service.meridian_knowledge_context(self.a.id, self.account_a.id))
        with self.assertRaises(UserError):
            service.meridian_knowledge_apply(
                self.a.id, "publish", page_id=page["id"], revision=page["revision"],
            )
        review = service.meridian_knowledge_apply(
            self.a.id, "review", page_id=page["id"], revision=page["revision"],
        )["page"]
        with self.assertRaises(AccessError):
            self.env["document.page"].with_user(self.editor).meridian_knowledge_apply(
                self.a.id, "publish", page_id=page["id"], revision=review["revision"],
            )
        published = service.meridian_knowledge_apply(
            self.a.id, "publish", page_id=page["id"], revision=review["revision"],
        )["page"]
        self.assertEqual(published["state"], "published")
        context = service.meridian_knowledge_context(self.a.id, self.account_a.id)
        self.assertEqual(context[0]["reference"], f"document.page:{page['id']}")
        with self.assertRaises(UserError):
            service.meridian_knowledge_apply(
                self.a.id,
                "save",
                self.values(),
                page_id=page["id"],
                revision=published["revision"],
            )

    def test_stale_revision_rejected(self):
        page = self.create_page()
        with self.assertRaises(UserError):
            self.env["document.page"].with_user(self.manager).meridian_knowledge_apply(
                self.a.id, "save", self.values(), page_id=page["id"], revision="stale",
            )

    def test_qbo_source_is_not_operational_even_with_l3_metadata(self):
        source = self.env["account.account"].create({
            "name": "QBO historical source", "code": "998803", "account_type": "expense",
            "company_ids": [(6, 0, [self.a.id])], "qbo_id": "historical-source",
            "poseidon_kernel_layer": "L3",
        })
        snapshot = self.env["document.page"].with_user(self.manager).meridian_knowledge_snapshot(self.a.id)
        self.assertNotIn(source.id, [a["id"] for a in snapshot["accounts"]])
        self.assertFalse(self.env["document.page"].with_user(self.manager).meridian_knowledge_context(self.a.id, source.id))
        with self.assertRaises(ValidationError):
            self.env["document.page"].with_user(self.manager).meridian_knowledge_snapshot(self.a.id, account_id=source.id)
        self.assertIn(self.account_a.id, [a["id"] for a in snapshot["accounts"]])
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self.create_page(account_ids=[source.id])

    def test_unclassified_company_account_is_not_a_kernel_fallback(self):
        account = self.env["account.account"].create({
            "name": "Unclassified company account", "code": "998804", "account_type": "expense",
            "company_ids": [(6, 0, [self.a.id])],
        })
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self.create_page(account_ids=[account.id])
