import json
from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, new_test_user, tagged


@tagged("post_install", "-at_install")
class TestPoseidonMCP(TransactionCase):
    def setUp(self):
        super().setUp()
        self.user = new_test_user(self.env, "poseidon_mcp_user")
        self.Job = self.env["kodoo.mcp.job"].sudo()

    def _catalog(self):
        return {
            entry["key"]: entry
            for entry in self.Job.get_operation_catalog()
        }

    def test_poseidon_health_check_descriptor_matches_contract(self):
        descriptor = self._catalog()["poseidon.health_check"]

        for field in (
            "key",
            "version",
            "label",
            "category",
            "bundle",
            "description",
            "available",
            "heavy",
            "supports_process_now",
            "requires_modules",
            "payload_outline",
            "result_outline",
        ):
            self.assertIn(field, descriptor, field)
        self.assertEqual(descriptor["bundle"], "poseidon")
        self.assertTrue(descriptor["available"])

    def test_poseidon_bundle_is_active(self):
        bundle = (
            self.env["kodoo.mcp.bundle"]
            .sudo()
            .with_context(active_test=False)
            .search([("key", "=", "poseidon")], limit=1)
        )

        self.assertTrue(bundle)
        self.assertTrue(bundle.active)

    def test_health_check_job_processes_to_done(self):
        job, created = self.Job.create_or_get_job(
            operation_key="poseidon.health_check",
            payload={},
            user=self.user,
        )

        self.assertTrue(created)
        job.action_process_now()

        payload = job.to_api_payload(include_payload=True)
        self.assertEqual(job.state, "done")
        self.assertEqual(payload["result"]["status"], "success")
        self.assertTrue(payload["result"]["details"]["kernel_ready"])

    def test_unknown_poseidon_operation_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.Job.get_operation_descriptor("poseidon.no_such_operation")

    def test_non_poseidon_operation_delegates_to_parent(self):
        job, _created = self.Job.create_or_get_job(
            operation_key="kodoo.echo",
            payload={"message": "delegated"},
            user=self.user,
        )

        job.action_process_now()

        self.assertEqual(job.state, "done")
        result = json.loads(job.result_json)
        self.assertEqual(result["echo"], {"message": "delegated"})

    def test_tool_catalog_exposes_poseidon_health_check(self):
        names = {tool["name"] for tool in self.Job.get_tool_catalog(self.user)}

        self.assertIn("poseidon__health_check", names)


@tagged("post_install", "-at_install")
class TestPoseidonL3MCPTools(TransactionCase):
    """Fase 8: L3 preview + approved apply with idempotency."""

    def setUp(self):
        super().setUp()
        self.Job = self.env["kodoo.mcp.job"].sudo()
        self.manager = new_test_user(
            self.env,
            "poseidon_l3_manager",
            groups="base.group_user,account.group_account_manager",
        )
        self.reader = new_test_user(self.env, "poseidon_l3_reader")

    def _catalog(self):
        return {
            entry["key"]: entry
            for entry in self.Job.get_operation_catalog()
        }

    def _company(self):
        with patch.object(
            type(self.env["res.company"]),
            "_create_internal_project_task",
            lambda self: None,
        ):
            return self.env["res.company"].create({"name": "L3 MCP Test Co"})

    def _manager_in(self, company):
        self.manager.write(
            {
                "company_id": company.id,
                "company_ids": [(6, 0, [company.id])],
            },
        )
        return self.manager

    def _run_job(self, operation_key, payload, user, idempotency_key=None):
        job, created = self.Job.create_or_get_job(
            operation_key=operation_key,
            payload=payload,
            user=user,
            idempotency_key=idempotency_key,
        )
        if created:
            job.action_process_now()
        return job, created

    def _company_accounts(self, company):
        return (
            self.env["account.account"]
            .with_company(company)
            .with_context(active_test=False)
            .search_count([("company_ids", "=", company.id)])
        )

    def test_l3_activation_descriptors_match_contract(self):
        catalog = self._catalog()

        for key in ("poseidon.preview_l3_activation", "poseidon.activate_l3"):
            descriptor = catalog[key]
            for field in (
                "key",
                "version",
                "label",
                "category",
                "bundle",
                "description",
                "available",
                "heavy",
                "supports_process_now",
                "requires_modules",
                "payload_outline",
                "result_outline",
            ):
                self.assertIn(field, descriptor, field)
            self.assertEqual(descriptor["bundle"], "poseidon")
            self.assertTrue(descriptor["available"])
            self.assertTrue(descriptor["supports_process_now"])
            self.assertIn("company_id", descriptor["payload_outline"]["required"])

    def test_tool_catalog_exposes_l3_preview_and_activate(self):
        names = {tool["name"] for tool in self.Job.get_tool_catalog(self.manager)}

        self.assertIn("poseidon__preview_l3_activation", names)
        self.assertIn("poseidon__activate_l3", names)

    def test_l3_preview_is_read_only(self):
        company = self._company()
        manager = self._manager_in(company)
        before = self._company_accounts(company)

        job, created = self._run_job(
            "poseidon.preview_l3_activation",
            {"company_id": company.id, "codes": ["15010", "15020"]},
            manager,
        )

        self.assertTrue(created)
        self.assertEqual(job.state, "done")
        result = job.to_api_payload(include_payload=True)["result"]
        self.assertTrue(result["preview"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["codes"], ["15010", "15020"])
        self.assertEqual(result["already_present"], [])
        self.assertEqual(result["blocked"], [])
        self.assertEqual(result["company_id"], company.id)
        self.assertEqual(self._company_accounts(company), before)

    def test_l3_apply_requires_accounting_manager(self):
        company = self._company()
        reader = self.reader
        reader.write(
            {
                "company_id": company.id,
                "company_ids": [(6, 0, [company.id])],
            },
        )

        job, _created = self._run_job(
            "poseidon.activate_l3",
            {"company_id": company.id, "codes": ["15010"]},
            reader,
        )

        self.assertEqual(job.state, "error")
        self.assertIn("accounting manager", job.error_message)
        self.assertEqual(self._company_accounts(company), 0)

    def test_l3_apply_creates_accounts_and_is_repeatable(self):
        company = self._company()
        manager = self._manager_in(company)

        job, _created = self._run_job(
            "poseidon.activate_l3",
            {"company_id": company.id, "codes": ["15010", "15020"]},
            manager,
        )

        self.assertEqual(job.state, "done")
        result = job.to_api_payload(include_payload=True)["result"]
        self.assertEqual(result["activated"], 2)
        self.assertEqual({account["action"] for account in result["accounts"]}, {"created"})
        self.assertEqual(
            {account["account"]["code"] for account in result["accounts"]},
            {"15010", "15020"},
        )
        self.assertEqual(job.target_model, "account.account")
        self.assertTrue(job.target_res_id)
        self.assertEqual(self._company_accounts(company), 2)

        # Re-apply with a fresh job: nothing duplicates, all linked/present.
        rerun, _created = self._run_job(
            "poseidon.activate_l3",
            {"company_id": company.id, "codes": ["15010"]},
            manager,
        )
        self.assertEqual(rerun.state, "done")
        rerun_result = rerun.to_api_payload(include_payload=True)["result"]
        self.assertEqual(rerun_result["activated"], 1)
        self.assertEqual(rerun_result["accounts"][0]["action"], "already_present")
        self.assertEqual(self._company_accounts(company), 2)

    def test_l3_apply_retry_with_same_idempotency_key_never_duplicates(self):
        company = self._company()
        manager = self._manager_in(company)
        payload = {"company_id": company.id, "codes": ["15010"]}

        first, created = self._run_job(
            "poseidon.activate_l3",
            payload,
            manager,
            idempotency_key="l3:test-key",
        )
        self.assertTrue(created)
        self.assertEqual(first.state, "done")
        self.assertEqual(first.idempotency_key, "l3:test-key")

        existing, created = self._run_job(
            "poseidon.activate_l3",
            payload,
            manager,
            idempotency_key="l3:test-key",
        )
        self.assertFalse(created)
        self.assertEqual(existing.id, first.id)
        self.assertEqual(existing.state, "done")
        self.assertEqual(self._company_accounts(company), 1)

        with self.assertRaises(ValidationError):
            self.Job.create_or_get_job(
                operation_key="poseidon.activate_l3",
                payload={"company_id": company.id, "codes": ["15020"]},
                user=manager,
                idempotency_key="l3:test-key",
            )

    def test_l3_company_outside_scope_fails(self):
        company = self._company()
        # manager stays on its default company: the new one is out of scope.
        job, _created = self._run_job(
            "poseidon.preview_l3_activation",
            {"company_id": company.id, "codes": ["15010"]},
            self.manager,
        )

        self.assertEqual(job.state, "error")
        self.assertIn("outside your allowed scope", job.error_message)

    def test_l3_unknown_code_fails_loudly(self):
        company = self._company()
        manager = self._manager_in(company)

        job, _created = self._run_job(
            "poseidon.preview_l3_activation",
            {"company_id": company.id, "codes": ["99999"]},
            manager,
        )

        self.assertEqual(job.state, "error")
        self.assertIn("99999", job.error_message)

    def test_l3_batch_preview_then_apply(self):
        company = self._company()
        manager = self._manager_in(company)
        payload = {
            "company_id": company.id,
            "batch_type": "activity_tag",
            "batch_key": "Services",
        }

        preview, _created = self._run_job(
            "poseidon.preview_l3_activation",
            payload,
            manager,
        )
        self.assertEqual(preview.state, "done")
        preview_result = preview.to_api_payload(include_payload=True)["result"]
        self.assertTrue(preview_result["preview"])
        self.assertGreater(preview_result["count"], 0)
        self.assertEqual(preview_result["source"]["kind"], "batch")
        self.assertEqual(preview_result["source"]["batch_type"], "activity_tag")
        self.assertEqual(self._company_accounts(company), 0)

        apply_job, _created = self._run_job(
            "poseidon.activate_l3",
            payload,
            manager,
        )
        self.assertEqual(apply_job.state, "done")
        apply_result = apply_job.to_api_payload(include_payload=True)["result"]
        self.assertEqual(apply_result["activated"], preview_result["count"])
        self.assertEqual(
            set(apply_result["accounts"][0].keys()) & {"action", "account"},
            {"action", "account"},
        )
        self.assertEqual(self._company_accounts(company), preview_result["count"])

    def test_l3_apply_stress_batch_of_fifty_is_idempotent(self):
        company = self._company()
        manager = self._manager_in(company)
        codes = self.env["qbo.standard.account"].sudo().search(
            [
                ("entry_type", "=", "detail"),
                ("kernel_layer", "=", "L3"),
                ("active", "=", True),
            ],
            order="code",
            limit=50,
        ).mapped("code")
        self.assertEqual(len(codes), 50)
        payload = {"company_id": company.id, "codes": codes}

        first, _created = self._run_job(
            "poseidon.activate_l3",
            payload,
            manager,
            idempotency_key="l3:stress-fifty",
        )
        self.assertEqual(first.state, "done")
        first_result = first.to_api_payload(include_payload=True)["result"]
        self.assertEqual(first_result["activated"], 50)
        self.assertEqual(self._company_accounts(company), 50)

        replay, created = self._run_job(
            "poseidon.activate_l3",
            payload,
            manager,
            idempotency_key="l3:stress-fifty",
        )
        self.assertFalse(created)
        self.assertEqual(replay.id, first.id)
        self.assertEqual(replay.state, "done")
        self.assertEqual(self._company_accounts(company), 50)
