from odoo.tests.common import TransactionCase, new_test_user, tagged


@tagged("post_install", "-at_install", "usgaap")
class TestOperationalRoles(TransactionCase):
    def test_manager_can_open_every_operational_tool(self):
        user = new_test_user(
            self.env,
            login="operational-manager@example.com",
            groups="meridian_saas.group_meridian_saas_manager",
        )
        for xmlid, model in (
            ("fleet.fleet_group_manager", "fleet.vehicle"),
            ("hr.group_hr_manager", "hr.employee"),
            ("hr_attendance.group_hr_attendance_manager", "hr.attendance"),
            ("hr_recruitment.group_hr_recruitment_manager", "hr.applicant"),
            ("kodoo_checklist.group_checklist_manager", "checklist.item"),
            ("lunch.group_lunch_manager", "lunch.product"),
            ("maintenance.group_equipment_manager", "maintenance.equipment"),
            ("project.group_project_manager", "project.project"),
            ("survey.group_survey_manager", "survey.survey"),
        ):
            self.assertTrue(user.has_group(xmlid), xmlid)
            self.assertTrue(self.env[model].with_user(user).has_access("read"), model)

        capabilities = self.env["res.users"].with_user(user).poseidon_operational_capabilities()
        self.assertTrue(all(capabilities.values()), capabilities)

    def test_activity_restricts_operational_tools(self):
        company = self.env["res.company"].create({"name": "Restricted Scope Co"})
        self.env["poseidon.us.tax.profile"].create(
            {
                "company_id": company.id,
                "activity_tag": "portfolio_management",
            }
        )
        user = new_test_user(
            self.env,
            login="portfolio-manager@example.com",
            groups="meridian_saas.group_meridian_saas_manager",
        )

        capabilities = (
            self.env["res.users"].with_user(user).poseidon_operational_capabilities(company.id)
        )
        self.assertTrue(capabilities["invoicing"])
        self.assertTrue(capabilities["project"])
        self.assertFalse(capabilities["people"])
        self.assertFalse(capabilities["fleet"])
