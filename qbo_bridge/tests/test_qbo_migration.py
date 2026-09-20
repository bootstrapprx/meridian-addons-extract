"""Tests for the 19.0.1.1.0 migration that sets sync_mode on OAuth-connected realms."""
import importlib.util
from pathlib import Path

from odoo.tests.common import TransactionCase

POST_MIGRATE = (
    Path(__file__).resolve().parent.parent / "migrations" / "19.0.1.1.0" / "post-migrate.py"
)


def load_post_migrate():
    """Import the real migration file so this test breaks when its SQL changes."""
    spec = importlib.util.spec_from_file_location("qbo_post_migrate", POST_MIGRATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestQboMigration(TransactionCase):
    def test_migration_promotes_oauth_realms_only(self):
        """The post-migrate must set sync_mode='pull_only' only for realms that
        have state='connected' AND a refresh_token.  File-upload realms created
        by the BFF have state='connected' but no refresh_token and must keep
        sync_mode='upload'."""
        Realm = self.env["qbo.realm"].sudo()

        oauth = Realm.create({
            "name": "OAuth realm",
            "realm_id": "OAUTH_1",
            "client_id": "test",
            "client_secret": "test",
            "sync_mode": "upload",
            "state": "connected",
            "refresh_token": "abc123",
        })
        upload = Realm.create({
            "name": "Upload realm",
            "realm_id": "UPLOAD_1",
            "client_id": "test",
            "client_secret": "test",
            "sync_mode": "upload",
            "state": "connected",
        })
        self.env.flush_all()

        load_post_migrate().migrate(self.env.cr, "19.0.1.0.0")

        oauth.invalidate_recordset()
        upload.invalidate_recordset()

        self.assertEqual(oauth.sync_mode, "pull_only")
        self.assertEqual(upload.sync_mode, "upload")
