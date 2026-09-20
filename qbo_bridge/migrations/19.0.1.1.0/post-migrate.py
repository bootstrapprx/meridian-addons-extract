import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    _logger.info("qbo_realm: setting sync_mode='pull_only' for OAuth-connected realms")
    cr.execute(
        "UPDATE qbo_realm SET sync_mode = 'pull_only' "
        "WHERE state = 'connected' AND refresh_token IS NOT NULL"
    )
    _logger.info("qbo_realm: migrated %s realm(s)", cr.rowcount)
