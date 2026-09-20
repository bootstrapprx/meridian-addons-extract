"""Tests for QBO app-review settings."""

from odoo.tests.common import TransactionCase


class TestQboSettings(TransactionCase):
    def test_settings_expose_intuit_app_urls(self):
        settings = self.env["res.config.settings"].create(
            # Empty callback domain: every URL, redirect included, comes from the
            # one public host.
            {"qbo_public_domain": "https://review.example.com/", "qbo_callback_domain": ""},
        )

        self.assertEqual(settings.qbo_host_domain, "review.example.com")
        self.assertEqual(settings.qbo_launch_url, "https://review.example.com/quickbooks/launch")
        self.assertEqual(settings.qbo_disconnect_url, "https://review.example.com/quickbooks/disconnect")
        self.assertEqual(settings.qbo_connect_url, "https://review.example.com/quickbooks/connect")
        self.assertEqual(settings.qbo_privacy_policy_url, "https://review.example.com/privacy-policy")
        self.assertEqual(settings.qbo_eula_url, "https://review.example.com/eula")
        self.assertEqual(settings.qbo_oauth_redirect_url, "https://review.example.com/api/qbo/callback")

    def test_callback_domain_only_moves_the_redirect_uri(self):
        settings = self.env["res.config.settings"].create({
            "qbo_public_domain": "meridian.kodoo.online",
            "qbo_callback_domain": "md.kodoo.online",
        })

        self.assertEqual(settings.qbo_host_domain, "meridian.kodoo.online")
        self.assertEqual(settings.qbo_launch_url, "https://meridian.kodoo.online/quickbooks/launch")
        self.assertEqual(settings.qbo_privacy_policy_url, "https://meridian.kodoo.online/privacy-policy")
        self.assertEqual(
            settings.qbo_oauth_redirect_url, "https://md.kodoo.online/api/qbo/callback"
        )

    def test_settings_actions_open_public_pages(self):
        settings = self.env["res.config.settings"].create(
            {"qbo_public_domain": "md.kodoo.online"},
        )

        action = settings.action_open_qbo_production_profile()

        self.assertEqual(action["type"], "ir.actions.act_url")
        self.assertEqual(action["target"], "new")
        self.assertEqual(action["url"], "https://md.kodoo.online/quickbooks/production-profile")
