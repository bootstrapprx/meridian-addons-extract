import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
import urllib.parse
from contextlib import suppress
from datetime import timedelta

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services.qbo_api_client import QBOApiClient
from ..services.qbo_readiness import blocking
from ..services.qbo_sync_engine import QBOSyncEngine
from odoo.addons.kodoo_legal.utils import callback_url

_logger = logging.getLogger(__name__)

QBO_AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
QBO_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
QBO_REVOKE_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
QBO_DISCOVERY_URL = "https://developer.api.intuit.com/.well-known/openid_sandbox_configuration"
QBO_OAUTH_STATE_SECRET_PARAM = "qbo_bridge.oauth_state_secret"
QBO_OAUTH_STATE_TTL_SECONDS = 60 * 60

SCOPES = "com.intuit.quickbooks.accounting"

# Placeholder pristine realms provisioned by the BFF before OAuth carry this
# prefix and are not a real Intuit company id. A pull may never target one.
QBO_REALM_PLACEHOLDER_PREFIX = "AUTO_"

# Legal suffixes that legitimately vary between the Odoo company name and the
# QuickBooks company name ("Fair Offers" vs "Fair Offers LLC"). Stripped before
# the name cross-check so a naming difference is not read as a different entity.
_COMPANY_LEGAL_SUFFIXES = frozenset({
    "inc", "incorporated", "llc", "llp", "lp", "ltd", "limited", "corp",
    "corporation", "co", "company", "plc", "gmbh", "ag", "sa", "sas", "sl",
    "bv", "nv", "pty", "kk", "spa", "srl", "sarl", "oy", "ab", "as", "aps",
})


def normalise_company_name(value):
    """Casefold/punctuation/suffix-normalise a company name for comparison."""
    text = (value or "").casefold()
    text = re.sub(r"[^\w\s]", " ", text)
    tokens = [token for token in text.split() if token and token not in _COMPANY_LEGAL_SUFFIXES]
    return " ".join(tokens)


def company_names_match(left, right):
    """Conservative match used only as a cross-check, never as identity.

    Identity is realm-first; this only catches the gross "you authorised a
    completely different company" case. It is deliberately lenient about legal
    suffixes and containment so legitimate naming variants pass.
    """
    normalised_left = normalise_company_name(left)
    normalised_right = normalise_company_name(right)
    if not normalised_left or not normalised_right:
        return False
    return (
        normalised_left == normalised_right
        or normalised_left in normalised_right
        or normalised_right in normalised_left
    )


class QboOAuthValidationError(UserError):
    """A bind-time OAuth validation failure carrying a stable error code.

    The controller maps ``code`` to the ``qbo_error`` query parameter so the
    operator sees a specific reason instead of a generic failure.
    """

    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or _("QuickBooks authorization was rejected (%s).") % code)


class QboRealm(models.Model):
    """Represents one QuickBooks Online company (realm).

    A realm holds the OAuth2 credentials for a single QBO company ID.
    Multiple Odoo companies can map to the same realm via qbo.company.mapping.
    """

    _name = "qbo.realm"
    _description = "QBO Realm (Company)"
    _order = "name"

    # ── Identity ──────────────────────────────────────────────────────────────
    name = fields.Char(string="Realm name", required=True)
    realm_id = fields.Char(
        string="QBO Company ID",
        required=True,
        help="The realmId returned by Intuit after OAuth authorization.",
    )

    # ── OAuth2 app credentials (stored per realm; use Intuit dev portal) ─────
    client_id = fields.Char(
        string="Client ID",
        required=True,
        default=lambda self: os.environ.get("QBO_CLIENT_ID", ""),
    )
    client_secret = fields.Char(
        string="Client secret",
        required=True,
        groups="base.group_system",
        default=lambda self: os.environ.get("QBO_CLIENT_SECRET", ""),
    )

    # ── OAuth2 tokens ─────────────────────────────────────────────────────────
    access_token = fields.Text(string="Access token", groups="base.group_system")
    refresh_token = fields.Text(string="Refresh token", groups="base.group_system")
    token_expiry = fields.Datetime(string="Token expiry")
    redirect_uri = fields.Char(
        string="Redirect URI",
        default=lambda self: self._default_redirect_uri(),
        help="Must match exactly what is registered in the Intuit developer portal.",
    )

    # ── Sync mode ─────────────────────────────────────────────────────────────
    sync_mode = fields.Selection(
        [
            ("upload", "File upload — import from QBO export files"),
            ("pull_only", "Live sync — pull QBO data via OAuth"),
        ],
        default="upload",
        string="Sync mode",
        help=(
            "upload = file-based import (no live API). "
            "pull_only = live OAuth sync (QBO → Odoo, never writes back). "
            "Automatically switches to pull_only after successful OAuth authorization."
        ),
    )

    # ── State ─────────────────────────────────────────────────────────────────
    state = fields.Selection(
        [
            ("draft", "Not connected"),
            ("connected", "Connected"),
            ("error", "Error"),
        ],
        default="draft",
        string="Status",
    )
    last_error = fields.Text(string="Last error", readonly=True)
    last_sync_date = fields.Datetime(string="Last sync", readonly=True)

    # ── Relations ─────────────────────────────────────────────────────────────
    mapping_ids = fields.One2many(
        "qbo.company.mapping", "realm_id", string="Company mappings",
    )

    # ── Sandbox toggle ────────────────────────────────────────────────────────
    is_sandbox = fields.Boolean(
        string="Sandbox mode",
        default=False,
        help="Use the QBO sandbox API endpoint instead of production.",
    )

    # ── Company binding safety ────────────────────────────────────────────────
    allow_company_name_mismatch = fields.Boolean(
        string="Allow company name mismatch",
        groups="qbo_bridge.group_qbo_bridge_manager",
        help="Manager-only escape hatch for a legitimate QuickBooks/Odoo naming "
        "difference. It only relaxes the name cross-check on first connect; the "
        "realmId echo and realm-ownership checks always apply and can never be "
        "overridden.",
    )

    # =========================================================================
    # Defaults
    # =========================================================================

    @api.model
    def _default_redirect_uri(self):
        # Workspace subdomain, not the control-plane domain: the callback has to
        # land on the database that holds this realm's tokens.
        return callback_url(self.env, fallback_to_base_url=True)

    @api.model
    def _oauth_state_secret(self):
        params = self.env["ir.config_parameter"].sudo()
        secret = params.get_param(QBO_OAUTH_STATE_SECRET_PARAM)
        if not secret:
            secret = secrets.token_urlsafe(32)
            params.set_param(QBO_OAUTH_STATE_SECRET_PARAM, secret)
        return secret

    @api.model
    def _sign_oauth_state_payload(self, payload):
        return hmac.new(
            self._oauth_state_secret().encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()

    def _resolve_connection_company(self, company_id=None):
        """Resolve the single Meridian company this connection belongs to.

        Authoritative on the backend: the company is derived from the realm's
        mapping, and a caller-supplied ``company_id`` is only accepted when it
        is one of the companies already mapped to this realm. A client value is
        never authority.
        """
        self.ensure_one()
        mapped = self.mapping_ids.mapped("company_id")
        if not mapped:
            raise UserError(_(
                "QuickBooks realm %(realm)s is not linked to a company yet."
            ) % {"realm": self.name})
        if company_id:
            company = self.env["res.company"].sudo().browse(int(company_id)).exists()
            if not company or company not in mapped:
                raise UserError(_(
                    "The selected company is not mapped to QuickBooks realm "
                    "%(realm)s."
                ) % {"realm": self.name})
            return company
        if len(mapped) == 1:
            return mapped
        raise UserError(_(
            "Select a company before connecting QuickBooks realm %(realm)s."
        ) % {"realm": self.name})

    def _encode_oauth_state(self, company_id=None):
        self.ensure_one()
        company = self._resolve_connection_company(company_id)
        payload = f"{self.id}:{int(time.time())}:{secrets.token_urlsafe(8)}:{company.id}"
        signature = self._sign_oauth_state_payload(payload)
        raw_state = f"{payload}:{signature}".encode()
        return base64.urlsafe_b64encode(raw_state).decode().rstrip("=")

    @api.model
    def _decode_oauth_state(self, state):
        """Return ``(realm, company_id)`` for a signed OAuth state.

        States minted before the company-bound format (four colon-separated
        parts) are still accepted for the duration of one TTL so an in-flight
        connect survives a deploy; ``company_id`` is ``None`` for those and the
        caller must tolerate it. Remove the legacy branch once the TTL window
        has elapsed.
        """
        try:
            padding = "=" * (-len(state) % 4)
            raw_state = base64.urlsafe_b64decode(f"{state}{padding}").decode()
            parts = raw_state.split(":")
            if len(parts) == 4:
                realm_id, issued_at, nonce, signature = parts
                payload = f"{realm_id}:{issued_at}:{nonce}"
                company_id = None
            elif len(parts) == 5:
                realm_id, issued_at, nonce, company_id, signature = parts
                payload = f"{realm_id}:{issued_at}:{nonce}:{company_id}"
            else:
                raise ValueError("malformed state")
            expected_signature = self._sign_oauth_state_payload(payload)
            if not hmac.compare_digest(signature, expected_signature):
                raise ValueError("signature mismatch")
            issued_at_ts = int(issued_at)
            if time.time() - issued_at_ts > QBO_OAUTH_STATE_TTL_SECONDS:
                raise ValueError("state expired")
            realm = self.sudo().browse(int(realm_id))
            if not realm.exists():
                raise ValueError("realm not found")
            return realm, (int(company_id) if company_id else None)
        except Exception as exc:
            raise UserError(_("Invalid or expired QBO OAuth state. Start the connection again.")) from exc

    # =========================================================================
    # Computed helpers
    # =========================================================================

    @api.depends("token_expiry")
    def _compute_token_valid(self):
        now = fields.Datetime.now()
        for rec in self:
            rec.token_valid = bool(rec.token_expiry and rec.token_expiry > now)

    token_valid = fields.Boolean(compute="_compute_token_valid", string="Token valid")

    @property
    def api_base_url(self):
        if self.is_sandbox:
            return f"https://sandbox-quickbooks.api.intuit.com/v3/company/{self.realm_id}"
        return f"https://quickbooks.api.intuit.com/v3/company/{self.realm_id}"

    # =========================================================================
    # OAuth2 flow
    # =========================================================================

    def action_get_authorization_url(self, company_id=None):
        """Build and return the Intuit OAuth2 authorization URL for the user to visit."""
        self.ensure_one()

        rec = self.sudo()
        if not rec.client_id or not rec.client_secret:
            raise UserError(_(
                "QBO Client ID and Client Secret are not configured. "
                "Set QBO_CLIENT_ID and QBO_CLIENT_SECRET in the runtime environment, "
                "or fill them in the QBO Realm form."
            ))

        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "scope": SCOPES,
            "redirect_uri": self.redirect_uri,
            "state": rec._encode_oauth_state(company_id),
        }
        url = f"{QBO_AUTH_URL}?{urllib.parse.urlencode(params)}"
        return {
            "type": "ir.actions.act_url",
            "url": url,
            "target": "new",
        }

    def action_exchange_code(self, code, qbo_realm_id=None, company_id=None):
        """Exchange the authorization code for access + refresh tokens.

        Called by the OAuth callback controller after the user grants access.
        The Intuit-returned ``qbo_realm_id`` is validated against this realm's
        identity before it is persisted; ``company_id`` comes from the signed
        state and is re-validated against the realm's mapping.
        """
        self.ensure_one()
        resp = requests.post(
            QBO_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
            },
            auth=(self.client_id, self.client_secret),
            headers={"Accept": "application/json"},
            timeout=15,
        )
        self._handle_token_response(resp, qbo_realm_id=qbo_realm_id, company_id=company_id)

    def _fetch_authorized_company(self, access_token):
        """Probe CompanyInfo with a freshly issued, not-yet-persisted token.

        Returns the CompanyInfo dict, or ``None`` when the probe is
        unreachable. A transport failure must not recreate the cross-company
        bug, but blocking a legitimate connect on a transient blip is also
        wrong: the caller proceeds with the remaining identity checks and logs
        the gap.
        """
        client = QBOApiClient(self, access_token=access_token)
        try:
            info = client.get_company_info()
        except Exception as exc:  # noqa: BLE001 — probe must never re-raise
            _logger.warning("QBO realm %s: companyinfo probe failed: %s", self.name, exc)
            return None
        return info.get("CompanyInfo") or {}

    def _validate_bind(self, qbo_realm_id, company_id, access_token):
        """Authoritative OAuth bind guard.

        Order matters: identity checks come first and can never be overridden;
        the company-name cross-check is last and is the only relaxable one.
        Returns the live QBO company name to attest, or ``None`` when no probe
        was possible.
        """
        qbo_realm_id = str(qbo_realm_id or "").strip()
        if not qbo_realm_id:
            raise QboOAuthValidationError("invalid_callback")

        # A realmId already held by another qbo.realm is cross-tenant linking.
        # Always blocked, never overridable.
        other = self.sudo().search(
            [("realm_id", "=", qbo_realm_id), ("id", "!=", self.id)], limit=1,
        )
        if other:
            raise QboOAuthValidationError("realm_taken")

        # A previously authorised realm must re-authorise to the same company.
        already_bound = bool(self.refresh_token)
        if already_bound and str(self.realm_id) != qbo_realm_id:
            raise QboOAuthValidationError("realm_mismatch")

        info = self._fetch_authorized_company(access_token)
        if info is None:
            _logger.warning(
                "QBO realm %s: companyinfo unavailable during bind; proceeding "
                "with realm identity checks only.", self.name,
            )
            return None

        live_id = str(info.get("Id") or "").strip()
        if live_id and live_id != qbo_realm_id:
            # The token issued for realm X answered as realm Y. Always blocked.
            raise QboOAuthValidationError("company_mismatch")

        live_name = str(info.get("CompanyName") or info.get("LegalName") or "").strip()

        if not already_bound and not self.allow_company_name_mismatch:
            company = self._resolve_connection_company(company_id)
            expected = company.name or ""
            if live_name and expected and not company_names_match(live_name, expected):
                self._set_error(_(
                    "Authorized QuickBooks company '%(live)s' does not match the "
                    "selected company '%(expected)s'. Nothing was connected."
                ) % {"live": live_name, "expected": expected})
                raise QboOAuthValidationError("company_mismatch")

        return live_name or None

    def _refresh_access_token(self):
        """Use the refresh token to obtain a new access token.

        Called automatically by QBOApiClient before each request when the
        access token has expired.
        """
        self.ensure_one()
        r = self.sudo()
        if not r.refresh_token:
            self._set_error("No refresh token available. Re-authorise the connection.")
            raise UserError(_("QBO realm %s has no refresh token. Please re-connect.") % r.name)

        resp = requests.post(
            QBO_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": r.refresh_token,
            },
            auth=(r.client_id, r.client_secret),
            headers={"Accept": "application/json"},
            timeout=15,
        )
        r._handle_token_response(resp)

    def _handle_token_response(self, resp, qbo_realm_id=None, company_id=None):
        try:
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            self._set_error(str(exc))
            raise UserError(_("QBO token exchange failed: %s") % exc) from exc

        # Validate the bind BEFORE persisting anything. On a mismatch the
        # freshly issued tokens are discarded and no realm_id is written, so a
        # wrong company can never become the persisted identity.
        attested_name = None
        if qbo_realm_id:
            attested_name = self._validate_bind(qbo_realm_id, company_id, data["access_token"])

        expiry = fields.Datetime.now() + timedelta(seconds=data.get("expires_in", 3600))
        values = {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", self.refresh_token),
            "token_expiry": expiry,
            "state": "connected",
            "sync_mode": "pull_only",
            "last_error": False,
        }
        if qbo_realm_id:
            values["realm_id"] = qbo_realm_id
        self.sudo().write(values)

        if attested_name:
            # Attest the live QBO company name on the mapping. The import
            # provenance guard compares future pulls against this attested
            # value, never against the Odoo company name.
            mappings = self.mapping_ids
            if company_id:
                mappings = mappings.filtered(lambda m: m.company_id.id == int(company_id))
            if mappings:
                mappings.sudo().write({"qbo_authorized_company_name": attested_name})

        _logger.info("QBO realm %s token refreshed; expires %s", self.name, expiry)

    def action_test_connection(self):
        """Ping the QBO company endpoint to verify connectivity."""
        self.ensure_one()

        client = QBOApiClient(self)
        try:
            info = client.get_company_info()
            company_name = info.get("CompanyInfo", {}).get("CompanyName", "?")
            self.state = "connected"
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Connected"),
                    "message": _("Successfully connected to QBO company: %s") % company_name,
                    "type": "success",
                },
            }
        except Exception as exc:
            self._set_error(str(exc))
            raise UserError(_("Connection test failed: %s") % exc) from exc

    def action_disconnect(self):
        """Revoke tokens and reset to draft."""
        self.ensure_one()
        if self.access_token:
            with suppress(Exception):
                requests.post(
                    QBO_REVOKE_URL,
                    json={"token": self.refresh_token or self.access_token},
                    auth=(self.client_id, self.client_secret),
                    headers={"Accept": "application/json"},
                    timeout=10,
                )
        self.sudo().write(
            {
                "access_token": False,
                "refresh_token": False,
                "token_expiry": False,
                "state": "draft",
                "last_error": False,
            },
        )

    # =========================================================================
    # Internal helpers
    # =========================================================================

    def _set_error(self, message):
        self.sudo().write({"state": "error", "last_error": message})
        _logger.error("QBO realm %s error: %s", self.name, message)

    # =========================================================================
    # Cron entry point
    # =========================================================================

    @api.model
    def cron_sync_all_realms(self):
        """Called by the scheduled action. Iterates all active mappings."""
        mappings = self.env["qbo.company.mapping"].sudo().search([
            ("sync_enabled", "=", True),
            ("realm_id.sync_mode", "=", "pull_only"),
            ("realm_id.state", "=", "connected"),
            ("realm_id.refresh_token", "!=", False),
        ])
        now = fields.Datetime.now()
        for mapping in mappings:
            # A manual trigger sets sync_requested and runs ASAP, ignoring the
            # interval throttle. Scheduled runs still respect the interval.
            requested = mapping.sync_requested
            if (
                not requested
                and mapping.sync_interval_minutes
                and mapping.last_sync_date
                and (now - mapping.last_sync_date).total_seconds()
                < (mapping.sync_interval_minutes * 60)
            ):
                continue
            try:
                readiness = mapping.get_sync_readiness()
                if readiness["state"] == "blocked":
                    # Skip loudly. A silent skip is indistinguishable from a
                    # cron that never ran, which is exactly the confusion this
                    # ticket exists to remove.
                    self.env["qbo.sync.log"].log(
                        self.env, mapping, "mapping", "pull", "skipped", "skip",
                        message="Pull skipped — mapping is blocked: %s"
                        % "; ".join(check["message"] for check in blocking(readiness)),
                    )
                    continue
                engine = QBOSyncEngine(self.env, mapping)
                engine.sync_all()
            except Exception:
                _logger.exception("Cron sync failed for mapping %s", mapping.display_name)
            finally:
                # Clear the manual-request flag whether or not the sync succeeded,
                # so a failed run does not loop the cron on the same mapping.
                if requested:
                    mapping.sync_requested = False
