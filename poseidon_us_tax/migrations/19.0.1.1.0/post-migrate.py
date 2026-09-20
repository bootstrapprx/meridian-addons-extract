"""Seed the MD workspace activity profiles versioned and idempotently."""

import logging

import odoo

_logger = logging.getLogger(__name__)

_COMPANY_ACTIVITIES = {
    "16x9 LLC": "marketing_consulting",
    "Alphabet Group LLC": "portfolio_management",
    "Capital Selector LLC": "credit_intermediation",
    "Domari Property Management LLC": "residential_property_management",
    "Ennovation Group LLC": "computer_systems_design",
    "Envision Trials LLC": "research_development",
    "Fair Offers LLC": "real_estate_other",
    "Maravilla Cleaners LLC": "janitorial_services",
    "Urbana Group LLC": "real_estate_other",
}


def migrate(cr, version):
    env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
    Profile = env["poseidon.us.tax.profile"]
    companies = env["res.company"].search(
        [("name", "in", list(_COMPANY_ACTIVITIES))],
        order="name",
    )
    created = updated = unchanged = 0
    for company in companies:
        activity = _COMPANY_ACTIVITIES[company.name]
        vals = {
            "activity_tag": activity,
            "subject_to_sales_tax": False,
            "subject_to_business_tax": True,
        }
        profile = Profile.search([("company_id", "=", company.id)], limit=1)
        if profile:
            if (
                profile.activity_tag == activity
                and not profile.subject_to_sales_tax
                and profile.subject_to_business_tax
            ):
                unchanged += 1
                continue
            profile.write(vals)
            updated += 1
        else:
            Profile.create({"company_id": company.id, **vals})
            created += 1
    _logger.info(
        "MD activity seed: created=%s updated=%s unchanged=%s",
        created,
        updated,
        unchanged,
    )
