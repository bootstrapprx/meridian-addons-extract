from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "poseidon_cost_center")
class TestProjectEconomics(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.center = cls.env["account.analytic.account"].browse(
            cls.env["account.analytic.account"].poseidon_create_cost_center({
                "code": "CC-PROJECT",
                "name": "Project Delivery",
                "company_id": cls.company.id,
            })["id"]
        )
        cls.center_column = cls.center.root_plan_id._column_name()
        cls.employee = cls.env["hr.employee"].create({
            "name": "Project Economist",
            "company_id": cls.company.id,
            "hourly_cost": 50.0,
        })

    def _project(self, name="Costed Project", center=True):
        values = {
            "name": name,
            "company_id": self.company.id,
            "allow_timesheets": True,
        }
        if center:
            values[self.center_column] = self.center.id
        return self.env["project.project"].create(values)

    def _timesheet(self, project, hours=2.0):
        return self.env["account.analytic.line"].create({
            "name": "Delivery work",
            "project_id": project.id,
            "employee_id": self.employee.id,
            "unit_amount": hours,
        })

    def test_timesheet_copies_project_and_cost_center_dimensions(self):
        project = self._project()
        timesheet = self._timesheet(project)
        self.assertEqual(timesheet.account_id, project.account_id)
        self.assertEqual(timesheet[self.center_column], self.center)
        self.assertEqual(timesheet.amount, -100.0)

    def test_timesheet_without_project_cost_center_is_blocked(self):
        project = self._project(name="Unassigned Project", center=False)
        with self.assertRaises(ValidationError):
            self._timesheet(project)

    def test_public_economics_contract_uses_native_profitability(self):
        project = self._project()
        self._timesheet(project)
        payload = self.env["project.project"].poseidon_project_economics(project.id)
        self.assertEqual(payload["project_id"], project.id)
        self.assertEqual(payload["cost_center"]["id"], self.center.id)
        self.assertEqual(payload["costs"]["billed"], 100.0)
        self.assertEqual(payload["costs"]["total"], 100.0)
        self.assertEqual(payload["margin"], -100.0)
        self.assertIn("budget", payload)
        self.assertIn("sections", payload)

    def test_setting_cost_center_is_a_validated_audited_backend_action(self):
        project = self._project(center=False)
        payload = self.env["project.project"].poseidon_set_project_cost_center(
            project.id,
            self.center.id,
        )
        self.assertEqual(project[self.center_column], self.center)
        self.assertEqual(payload["cost_center"]["id"], self.center.id)
        self.assertTrue(project.message_ids.filtered(
            lambda message: "Cost center changed" in (message.body or "")
        ))
