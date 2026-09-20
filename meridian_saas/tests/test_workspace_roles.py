"""Meridian workspace roles — the permission truth this module owns.

The BFF assigns a role by NAME and Odoo resolves the rest through
``implied_ids``.  These tests guard that contract in the database, because a
missing implication or a role group left out of the managed-strip list does not
raise anywhere: it silently grants or keeps permissions the role never meant to
carry.
"""

import inspect

from odoo.tests.common import TransactionCase, tagged

from ..controllers.meridian_saas_controller import (
    _MERIDIAN_MANAGED_XMLIDS,
    _ROLE_GROUPS,
    MeridianSaasController,
)


@tagged("post_install", "-at_install", "meridian_saas")
class TestMeridianWorkspaceRoles(TransactionCase):

    def test_role_names_are_exactly_the_five_meridian_roles(self):
        self.assertEqual(
            sorted(_ROLE_GROUPS),
            ["accountant", "invoicing", "manager", "member", "viewer"],
        )

    def test_every_role_maps_to_one_installed_group(self):
        for role, xmlids in _ROLE_GROUPS.items():
            self.assertEqual(len(xmlids), 1, f"{role} must map to exactly one group")
            group = self.env.ref(xmlids[0], raise_if_not_found=False)
            self.assertTrue(group, f"{role} points at a group that is not installed")

    def test_every_role_group_is_in_the_managed_strip_list(self):
        # A role group missing here survives a role change, so a demoted user
        # keeps the wider role's permissions.
        for role, xmlids in _ROLE_GROUPS.items():
            for xmlid in xmlids:
                self.assertIn(
                    xmlid,
                    _MERIDIAN_MANAGED_XMLIDS,
                    f"{role}'s group must be stripped before a new role is applied",
                )

    def test_every_role_implies_the_internal_user_group(self):
        internal = self.env.ref("base.group_user")
        for role, xmlids in _ROLE_GROUPS.items():
            group = self.env.ref(xmlids[0])
            implied = group.implied_ids | group
            self.assertIn(
                internal.id,
                implied.ids,
                f"{role} must be an internal user",
            )

    def test_every_legacy_raw_role_group_is_stripped_on_role_change(self):
        self.assertTrue(
            {
                "base.group_system",
                "base.group_erp_manager",
                "base.group_partner_manager",
                "account.group_account_manager",
                "account.group_account_user",
                "sales_team.group_sale_manager",
                "sales_team.group_sale_salesman",
                "stock.group_stock_manager",
                "stock.group_stock_user",
            }.issubset(_MERIDIAN_MANAGED_XMLIDS)
        )

    def test_roles_carry_their_accounting_chain(self):
        expected = {
            "viewer": "account.group_account_readonly",
            "invoicing": "account.group_account_invoice",
            "member": "account.group_account_user",
            "accountant": "account.group_account_manager",
            "manager": "account.group_account_manager",
        }
        for role, account_group_xmlid in expected.items():
            group = self.env.ref(_ROLE_GROUPS[role][0])
            account_group = self.env.ref(account_group_xmlid, raise_if_not_found=False)
            if not account_group:
                continue
            self.assertIn(
                account_group.id,
                (group.implied_ids | group).ids,
                f"{role} must imply {account_group_xmlid}",
            )

    def test_manager_implies_contact_and_entity_administration(self):
        manager = self.env.ref(_ROLE_GROUPS["manager"][0])
        closure = (manager.implied_ids | manager).ids
        for xmlid in ("base.group_erp_manager", "base.group_partner_manager"):
            group = self.env.ref(xmlid, raise_if_not_found=False)
            self.assertTrue(group, f"{xmlid} must exist")
            self.assertIn(group.id, closure, f"manager must imply {xmlid}")

    def test_manager_implies_the_operational_module_groups_when_installed(self):
        """usgaap/security/meridian_operational_roles.xml extends Manager.

        Only asserted for modules actually installed in this database, so the
        test is meaningful on a profile that ships a subset.
        """
        manager = self.env.ref(_ROLE_GROUPS["manager"][0])
        closure = (manager.implied_ids | manager).ids
        for xmlid in (
            "fleet.fleet_group_manager",
            "hr.group_hr_manager",
            "hr_attendance.group_hr_attendance_manager",
            "hr_recruitment.group_hr_recruitment_manager",
            "lunch.group_lunch_manager",
            "maintenance.group_equipment_manager",
            "project.group_project_manager",
            "survey.group_survey_manager",
        ):
            group = self.env.ref(xmlid, raise_if_not_found=False)
            if not group:
                continue
            self.assertIn(group.id, closure, f"manager must imply {xmlid}")

    def test_update_workspace_user_accepts_a_role_and_refuses_raw_groups(self):
        """The endpoint must not take ``group_ids``.

        Raw group commands let a caller compose permission sets this module never
        sanctioned; the role name is the only supported input.
        """
        source = inspect.getsource(MeridianSaasController.update_workspace_user)
        self.assertIn("role=None", source, "the endpoint must accept a role name")
        self.assertIn('safe_keys = {"name", "active", "company_id", "company_ids"}', source)
        self.assertNotIn('"group_ids"', source.split("safe_keys")[1])
        self.assertIn("_apply_role_groups", source, "role changes go through the shared strip-and-apply")

    def test_unknown_role_is_rejected_before_any_write(self):
        controller = MeridianSaasController()
        # _resolve_role_groups is the only translation from name to groups; an
        # unknown name must produce nothing rather than a partial grant.
        self.assertEqual(_ROLE_GROUPS.get("admin"), None)
        self.assertEqual(_ROLE_GROUPS.get("sales"), None)
        self.assertTrue(callable(controller._resolve_role_groups))

        source = inspect.getsource(MeridianSaasController.update_workspace_user)
        self.assertLess(
            source.index("target_groups = self._resolve_role_groups"),
            source.index("user.write"),
        )
        self.assertIn("with request.env.cr.savepoint():", source)
        self.assertIn("if user_id <= 4:", source)
