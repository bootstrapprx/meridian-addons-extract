"""HTTP controller that handles the OAuth2 redirect callback from Intuit.

Intuit redirects the user's browser to the registered callback URL after
authorization. Public Poseidon installs use /api/qbo/callback, while this
controller keeps the backend callback available for direct Odoo deployments.
"""
import logging
import urllib.parse

from odoo import http
from odoo.exceptions import UserError
from odoo.http import request

from ..models.qbo_realm import QboOAuthValidationError

_logger = logging.getLogger(__name__)


class QboOAuthController(http.Controller):

    def _qbo_result_url(self, connected, reason=None):
        params = {"connected": connected}
        if reason:
            params["qbo_error"] = reason
        return f"/qbo?{urllib.parse.urlencode(params)}"

    def _realm_list_url(self):
        action = request.env.ref("qbo_bridge.action_qbo_realm", raise_if_not_found=False)
        if not action:
            return "/web"
        return f"/web#action={action.id}&model=qbo.realm&view_type=list"

    def _realm_form_url(self, realm):
        action = request.env.ref("qbo_bridge.action_qbo_realm", raise_if_not_found=False)
        if not action:
            return "/web"
        return (
            f"/web#action={action.id}&id={realm.id}"
            f"&model=qbo.realm&view_type=form"
        )

    @http.route(
        ["/qbo/callback", "/api/qbo/callback"],
        type="http",
        auth="public",
        methods=["GET"],
        save_session=False,
    )
    def oauth_callback(self, **kwargs):
        code = kwargs.get("code")
        state = kwargs.get("state")
        qbo_realm_id = kwargs.get("realmId") or kwargs.get("realm_id")
        error = kwargs.get("error")

        if error:
            _logger.warning("QBO OAuth error: %s — %s", error, kwargs.get("error_description"))
            return request.redirect(self._qbo_result_url("error", "intuit_oauth_error"))

        if not code or not state or not qbo_realm_id:
            return request.redirect(self._qbo_result_url("error", "invalid_callback"))

        try:
            realm, state_company_id = request.env["qbo.realm"].sudo()._decode_oauth_state(state)
        except UserError:
            _logger.warning("QBO callback: invalid or expired state")
            return request.redirect(self._qbo_result_url("error", "invalid_state"))

        # The signed state is the authority for the initiating company. A
        # company_id forwarded by the BFF is only accepted when it agrees with
        # the signed value; it can never change the binding.
        company_id = state_company_id
        forwarded_company_id = kwargs.get("company_id")
        if forwarded_company_id:
            try:
                forwarded_company_id = int(forwarded_company_id)
            except (TypeError, ValueError):
                return request.redirect(self._qbo_result_url("error", "invalid_callback"))
            if company_id and int(company_id) != forwarded_company_id:
                _logger.warning("QBO callback: forwarded company does not match signed state")
                return request.redirect(self._qbo_result_url("error", "company_mismatch"))
            company_id = company_id or forwarded_company_id

        # Re-validate the resolved company against the realm's current mapping.
        try:
            realm.sudo()._resolve_connection_company(company_id)
        except UserError:
            _logger.warning("QBO callback: company is not mapped to this realm")
            return request.redirect(self._qbo_result_url("error", "company_mismatch"))

        try:
            realm.sudo().action_exchange_code(
                code, qbo_realm_id=qbo_realm_id, company_id=company_id,
            )
            _logger.info("QBO realm %s authorised successfully", realm.name)
        except QboOAuthValidationError as exc:
            _logger.warning("QBO callback rejected (%s) for realm %s", exc.code, realm.name)
            return request.redirect(self._qbo_result_url("error", exc.code))
        except Exception:
            _logger.exception("QBO token exchange failed for realm %s", realm.name)
            return request.redirect(self._qbo_result_url("error", "token_exchange_failed"))

        return request.redirect(self._qbo_result_url("1"))
