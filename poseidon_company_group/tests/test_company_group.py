from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase


class TestPoseidonCompanyGroup(TransactionCase):
    def test_company_cannot_join_two_active_groups(self):
        company = self.env["res.company"].create({"name": "Group Constraint Company"})
        first = self.env["poseidon.company.group"].create({"name": "First Group"})
        second = self.env["poseidon.company.group"].create({"name": "Second Group"})
        self.env["poseidon.group.member"].create(
            {"group_id": first.id, "company_id": company.id}
        )
        with self.assertRaises(ValidationError):
            self.env["poseidon.group.member"].create(
                {"group_id": second.id, "company_id": company.id}
            )

        first.active = False
        self.env["poseidon.group.member"].create(
            {"group_id": second.id, "company_id": company.id}
        )
        with self.assertRaises(ValidationError):
            first.active = True
