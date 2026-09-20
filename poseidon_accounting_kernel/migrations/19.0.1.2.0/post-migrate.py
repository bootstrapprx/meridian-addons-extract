"""Seed the L3 analytic derived kernel on existing databases.

Existing DBs upgraded to 19.0.1.2.0 get the L3 expansion lazily seeded into the
master chart (the L0/L1 records were already materialized by the 19.0.1.1.0 data
files). The seed is idempotent and lenient: a missing L3 artifact simply leaves
the L3 expansion unavailable without blocking the upgrade.
"""

import logging

import odoo

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
    if "qbo.standard.account" not in env:
        return
    stats = env["qbo.standard.account"]._ensure_l3_master_chart_imported()
    if stats.get("available"):
        _logger.info(
            "L3 analytic kernel seed: created=%s updated=%s preserved=%s",
            stats.get("created", 0),
            stats.get("updated", 0),
            stats.get("preserved", 0),
        )
    else:
        _logger.warning(
            "L3 analytic kernel seed skipped: artifact %s not found; "
            "L3 accounts will remain unavailable.",
            "kernel_v2025_3_L3_derived.json",
        )
