from odoo import api, fields, models
from odoo.addons.kodoo_legal.utils import (
    CALLBACK_DOMAIN_PARAM,
    DEFAULT_ACCEPTED_CONNECTIONS,
    DEFAULT_HOSTING_COUNTRIES,
    DEFAULT_HOSTING_IP_ADDRESSES,
    DEFAULT_REGULATED_INDUSTRIES,
    DEFAULT_SERVICE_DOMAIN,
    QUICKBOOKS_ACCEPTED_CONNECTIONS_PARAM,
    QUICKBOOKS_BASE_PATH,
    QUICKBOOKS_HOSTING_COUNTRIES_PARAM,
    QUICKBOOKS_HOSTING_IP_ADDRESSES_PARAM,
    QUICKBOOKS_OAUTH_CALLBACK_PATH,
    QUICKBOOKS_REGULATED_INDUSTRIES_PARAM,
    SERVICE_DOMAIN_PARAM,
    normalize_domain,
    service_domain,
)


def _lines(values):
    return "\n".join(values)


QBO_REVIEW_PROFILE_PARAMS = {
    "qbo_hosting_countries": (
        QUICKBOOKS_HOSTING_COUNTRIES_PARAM,
        DEFAULT_HOSTING_COUNTRIES,
    ),
    "qbo_hosting_ip_addresses": (
        QUICKBOOKS_HOSTING_IP_ADDRESSES_PARAM,
        DEFAULT_HOSTING_IP_ADDRESSES,
    ),
    "qbo_accepted_connections": (
        QUICKBOOKS_ACCEPTED_CONNECTIONS_PARAM,
        DEFAULT_ACCEPTED_CONNECTIONS,
    ),
    "qbo_regulated_industries": (
        QUICKBOOKS_REGULATED_INDUSTRIES_PARAM,
        DEFAULT_REGULATED_INDUSTRIES,
    ),
}


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    qbo_public_domain = fields.Char(
        string="Public host domain",
        config_parameter=SERVICE_DOMAIN_PARAM,
        default=DEFAULT_SERVICE_DOMAIN,
        help="Public HTTPS host used by the Intuit app profile. Enter the domain without https://.",
    )
    qbo_callback_domain = fields.Char(
        string="OAuth callback domain",
        config_parameter=CALLBACK_DOMAIN_PARAM,
        help=(
            "Workspace host that receives the Intuit OAuth redirect, without https://. "
            "Leave empty to reuse the public host domain. Set it when the app is launched "
            "from a control-plane domain but the callback must land on a tenant subdomain."
        ),
    )
    qbo_hosting_countries = fields.Text(
        string="Hosting countries",
        default=lambda self: _lines(DEFAULT_HOSTING_COUNTRIES),
        help="One country per line for the Intuit production profile.",
    )
    qbo_hosting_ip_addresses = fields.Text(
        string="Hosting IP addresses",
        default=lambda self: _lines(DEFAULT_HOSTING_IP_ADDRESSES),
        help="One public production IP address per line, when applicable.",
    )
    qbo_accepted_connections = fields.Text(
        string="Accepted connections",
        default=lambda self: _lines(DEFAULT_ACCEPTED_CONNECTIONS),
        help="One accepted connection or sync pattern per line for Intuit review.",
    )
    qbo_regulated_industries = fields.Text(
        string="Regulated industries",
        default=lambda self: _lines(DEFAULT_REGULATED_INDUSTRIES),
        help="One regulated-industry note per line for the Intuit production profile.",
    )

    qbo_host_domain = fields.Char(compute="_compute_qbo_app_urls")
    qbo_setup_hub_url = fields.Char(compute="_compute_qbo_app_urls")
    qbo_production_profile_url = fields.Char(compute="_compute_qbo_app_urls")
    qbo_connect_url = fields.Char(compute="_compute_qbo_app_urls")
    qbo_launch_url = fields.Char(compute="_compute_qbo_app_urls")
    qbo_disconnect_url = fields.Char(compute="_compute_qbo_app_urls")
    qbo_privacy_policy_url = fields.Char(compute="_compute_qbo_app_urls")
    qbo_eula_url = fields.Char(compute="_compute_qbo_app_urls")
    qbo_oauth_redirect_url = fields.Char(compute="_compute_qbo_app_urls")

    @api.model
    def get_values(self):
        res = super().get_values()
        params = self.env["ir.config_parameter"].sudo()
        for field_name, (param_key, defaults) in QBO_REVIEW_PROFILE_PARAMS.items():
            res[field_name] = params.get_param(param_key, _lines(defaults)) or _lines(defaults)
        return res

    def set_values(self):
        super().set_values()
        params = self.env["ir.config_parameter"].sudo()
        for field_name, (param_key, _defaults) in QBO_REVIEW_PROFILE_PARAMS.items():
            params.set_param(param_key, (self[field_name] or "").strip())

    def _qbo_public_domain_for_preview(self):
        self.ensure_one()
        return normalize_domain(self.qbo_public_domain) or service_domain(self.env)

    def _qbo_callback_domain_for_preview(self):
        self.ensure_one()
        # qbo_callback_domain already carries the stored parameter, so an empty
        # value means "no split" — reuse whatever host this form is previewing,
        # never the packaged DEFAULT_SERVICE_DOMAIN.
        return normalize_domain(self.qbo_callback_domain) or self._qbo_public_domain_for_preview()

    @api.depends("qbo_public_domain", "qbo_callback_domain")
    def _compute_qbo_app_urls(self):
        for rec in self:
            domain = rec._qbo_public_domain_for_preview()
            base_url = f"https://{domain}"
            rec.qbo_host_domain = domain
            rec.qbo_setup_hub_url = f"{base_url}{QUICKBOOKS_BASE_PATH}"
            rec.qbo_production_profile_url = f"{base_url}{QUICKBOOKS_BASE_PATH}/production-profile"
            rec.qbo_connect_url = f"{base_url}{QUICKBOOKS_BASE_PATH}/connect"
            rec.qbo_launch_url = f"{base_url}{QUICKBOOKS_BASE_PATH}/launch"
            rec.qbo_disconnect_url = f"{base_url}{QUICKBOOKS_BASE_PATH}/disconnect"
            rec.qbo_privacy_policy_url = f"{base_url}/privacy-policy"
            rec.qbo_eula_url = f"{base_url}/eula"
            callback_base = f"https://{rec._qbo_callback_domain_for_preview()}"
            rec.qbo_oauth_redirect_url = f"{callback_base}{QUICKBOOKS_OAUTH_CALLBACK_PATH}"

    def _open_qbo_public_path(self, path):
        self.ensure_one()
        return {
            "type": "ir.actions.act_url",
            "target": "new",
            "url": f"https://{self._qbo_public_domain_for_preview()}{path}",
        }

    def action_open_qbo_setup_hub(self):
        return self._open_qbo_public_path(QUICKBOOKS_BASE_PATH)

    def action_open_qbo_production_profile(self):
        return self._open_qbo_public_path(f"{QUICKBOOKS_BASE_PATH}/production-profile")

    def action_open_qbo_connect_page(self):
        return self._open_qbo_public_path(f"{QUICKBOOKS_BASE_PATH}/connect")

    def action_open_qbo_launch_page(self):
        return self._open_qbo_public_path(f"{QUICKBOOKS_BASE_PATH}/launch")

    def action_open_qbo_disconnect_page(self):
        return self._open_qbo_public_path(f"{QUICKBOOKS_BASE_PATH}/disconnect")
