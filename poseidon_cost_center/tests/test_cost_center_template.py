import unittest

from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "poseidon_cost_center")
class TestCostCenterTemplate(TransactionCase):
    """Per-activity cost-center templates.

    The template says what a company of a given kind almost always needs; it is
    never a rule. What matters here is that seeding is safe to re-run on an
    established company: it must fill gaps without touching a center the
    operator has already made their own.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The activity catalog lives in poseidon_us_tax, which this module
        # deliberately does not depend on: the runtime lookup is soft and
        # returns `available: False` without it. The test mirrors that contract
        # instead of forcing an install-time dependency.
        if "poseidon.us.tax.profile" not in cls.env:
            raise unittest.SkipTest("poseidon_us_tax is not installed in this database.")
        cls.company = cls.env.company
        cls.plan = cls.env.ref("poseidon_cost_center.analytic_plan_cost_center")
        cls.Analytic = cls.env["account.analytic.account"]
        cls.Profile = cls.env["poseidon.us.tax.profile"]

    def _profile(self, activity_tag):
        profile = self.Profile.search([("company_id", "=", self.company.id)], limit=1)
        if profile:
            profile.activity_tag = activity_tag
            return profile
        return self.Profile.create(
            {"company_id": self.company.id, "activity_tag": activity_tag}
        )

    def _centers(self):
        return self.Analytic.with_context(active_test=False).search(
            [("company_id", "=", self.company.id), ("root_plan_id", "=", self.plan.id)]
        )

    def test_seeds_the_template_for_the_company_activity(self):
        self._profile("janitorial_services")
        result = self.Analytic.poseidon_seed_cost_center_template(self.company.id)
        self.assertTrue(result["available"])
        self.assertEqual(result["activity_tag"], "janitorial_services")
        codes = {item["code"] for item in result["created"]}
        self.assertEqual(codes, {"FLD", "SUP", "SLS", "ADM"})
        # Field operations is a department; supplies is shared across jobs.
        seeded = {center.code: center for center in self._centers()}
        self.assertEqual(seeded["SUP"].poseidon_cost_center_type, "shared_service")
        self.assertEqual(seeded["ADM"].poseidon_cost_center_type, "administrative")

    def test_running_twice_creates_nothing_and_raises_nothing(self):
        self._profile("marketing_consulting")
        first = self.Analytic.poseidon_seed_cost_center_template(self.company.id)
        self.assertTrue(first["created"])
        before = len(self._centers())
        second = self.Analytic.poseidon_seed_cost_center_template(self.company.id)
        self.assertEqual(second["created"], [])
        self.assertEqual(len(second["existing"]), before)
        self.assertEqual(len(self._centers()), before)

    def test_an_existing_code_is_never_renamed_or_retyped(self):
        # The operator may have repurposed a code deliberately. Seeding fills
        # gaps; it does not reconcile.
        self._profile("software")
        self.Analytic.poseidon_create_cost_center(
            {
                "code": "ENG",
                "name": "Platform team (ours)",
                "company_id": self.company.id,
                "poseidon_cost_center_type": "project",
            }
        )
        self.Analytic.poseidon_seed_cost_center_template(self.company.id)
        kept = self._centers().filtered(lambda center: center.code == "ENG")
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept.name, "Platform team (ours)")
        self.assertEqual(kept.poseidon_cost_center_type, "project")

    def test_dry_run_previews_without_writing(self):
        self._profile("portfolio_management")
        before = len(self._centers())
        preview = self.Analytic.poseidon_seed_cost_center_template(
            self.company.id, dry_run=True
        )
        self.assertTrue(preview["dry_run"])
        self.assertEqual({item["code"] for item in preview["created"]},
                         {"INV", "RES", "CMP", "ADM"})
        self.assertEqual(len(self._centers()), before)

    def test_an_unclassified_company_still_gets_a_usable_split(self):
        self._profile("unknown")
        result = self.Analytic.poseidon_seed_cost_center_template(self.company.id)
        self.assertEqual({item["code"] for item in result["created"]},
                         {"OPS", "SLS", "ADM"})

    def test_every_activity_has_a_template_and_every_row_is_well_formed(self):
        types = dict(
            self.Analytic._fields["poseidon_cost_center_type"].selection
        )
        templates = self.Profile.cost_center_templates()
        catalog = {item["value"] for item in self.Profile.activity_catalog()}
        self.assertEqual(set(templates), catalog, "every activity needs a template")
        for activity, rows in templates.items():
            self.assertTrue(rows, f"{activity} has no cost centers")
            codes = [row["code"] for row in rows]
            self.assertEqual(len(codes), len(set(codes)), f"{activity} repeats a code")
            for row in rows:
                self.assertIn(row["type"], types, f"{activity}/{row['code']} bad type")
                self.assertTrue(row["name"].strip(), f"{activity}/{row['code']} unnamed")
            # Overhead is named everywhere, which is what keeps it out of the
            # operating centers.
            self.assertIn("ADM", codes, f"{activity} has no administration center")

    def test_the_catalog_carries_the_template_for_the_ui_to_preview(self):
        entry = next(
            item
            for item in self.Profile.activity_catalog()
            if item["value"] == "credit_intermediation"
        )
        self.assertEqual(
            {row["code"] for row in entry["cost_centers"]},
            {"ORG", "UW", "SRV", "CMP", "ADM"},
        )
