"""Tests for QBO realm field defaults."""

import os
from unittest.mock import patch

from odoo.tests.common import TransactionCase


class TestQboClientCredentialDefaults(TransactionCase):
    """The OAuth app credentials come from Odoo's own environment.

    Callers must NOT pass client_id/client_secret themselves: an explicit value
    (including "") suppresses the default, which is how the BFF was silently
    creating realms with blank credentials.
    """

    def test_defaults_read_the_odoo_environment(self):
        with patch.dict(os.environ, {"QBO_CLIENT_ID": "app-123", "QBO_CLIENT_SECRET": "shh"}):
            defaults = self.env["qbo.realm"].default_get(["client_id", "client_secret"])

        self.assertEqual(defaults["client_id"], "app-123")
        self.assertEqual(defaults["client_secret"], "shh")

    def test_created_realm_inherits_env_credentials(self):
        with patch.dict(os.environ, {"QBO_CLIENT_ID": "app-123", "QBO_CLIENT_SECRET": "shh"}):
            realm = self.env["qbo.realm"].create({
                "name": "Upload realm",
                "realm_id": "AUTO_1",
            })

        self.assertEqual(realm.client_id, "app-123")
        self.assertEqual(realm.client_secret, "shh")

    def test_defaults_are_empty_without_env(self):
        with patch.dict(os.environ):
            os.environ.pop("QBO_CLIENT_ID", None)
            os.environ.pop("QBO_CLIENT_SECRET", None)
            defaults = self.env["qbo.realm"].default_get(["client_id", "client_secret"])

        self.assertEqual(defaults["client_id"], "")
        self.assertEqual(defaults["client_secret"], "")


class TestQboRedirectUri(TransactionCase):
    def setUp(self):
        super().setUp()
        self.params = self.env["ir.config_parameter"].sudo()
        self.params.set_param("kodoo_legal.callback_domain", "")

    def test_default_redirect_uri_uses_kodoo_legal_public_domain(self):
        self.params.set_param("kodoo_legal.public_domain", "md.kodoo.online")

        redirect_uri = self.env["qbo.realm"]._default_redirect_uri()

        self.assertEqual(redirect_uri, "https://md.kodoo.online/api/qbo/callback")

    def test_callback_domain_overrides_the_customer_facing_domain(self):
        """Intuit sends customers to the control plane but redirects OAuth to
        the workspace subdomain that stores the tokens."""
        self.params.set_param("kodoo_legal.public_domain", "meridian.kodoo.online")
        self.params.set_param("kodoo_legal.callback_domain", "md.kodoo.online")

        redirect_uri = self.env["qbo.realm"]._default_redirect_uri()

        self.assertEqual(redirect_uri, "https://md.kodoo.online/api/qbo/callback")

    def test_default_redirect_uri_falls_back_to_web_base_url(self):
        self.params.set_param("kodoo_legal.public_domain", "")
        self.params.set_param("web.base.url", "https://fallback.kodoo.online")

        redirect_uri = self.env["qbo.realm"]._default_redirect_uri()

        self.assertEqual(redirect_uri, "https://fallback.kodoo.online/api/qbo/callback")
