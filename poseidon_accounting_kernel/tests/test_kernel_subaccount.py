from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "poseidon_kernel")
class TestKernelSubaccount(TransactionCase):
    def test_subaccount_inherits_nature_and_keeps_kernel_as_parent(self):
        parent = self.env["account.account"].create({
            "name": "Ponytail expense aggregator",
            "code": "900001",
            "account_type": "expense",
            "company_ids": [(6, 0, [self.env.company.id])],
            "poseidon_kernel_layer": "L1",
        })

        result = self.env["account.account"].poseidon_create_subaccount(
            parent.id,
            self.env.company.id,
            "900002",
            "Ponytail operating expense",
        )
        child = self.env["account.account"].browse(result["id"])

        self.assertEqual(child.account_type, parent.account_type)
        self.assertEqual(child.poseidon_parent_account_id, parent)
        self.assertFalse(child.poseidon_kernel_layer)
