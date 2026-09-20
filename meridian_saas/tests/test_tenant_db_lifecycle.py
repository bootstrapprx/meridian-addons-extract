import datetime
from unittest.mock import patch
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import mute_logger
from odoo import fields

@tagged("post_install", "-at_install", "meridian_saas")
class TestTenantDbLifecycle(TransactionCase):

    def setUp(self):
        super().setUp()
        self.tdb_model = self.env["meridian.saas.tenant_db"]

    @patch("odoo.addons.meridian_saas.models.meridian_saas_tenant_db.MeridianSaasTenantDb._drop_tenant_db")
    @patch("odoo.addons.meridian_saas.models.meridian_saas_tenant_db.MeridianSaasTenantDb._backup_tenant_db")
    @patch("odoo.addons.meridian_saas.models.meridian_saas_tenant_db.MeridianSaasTenantDb._tenant_last_activity")
    def test_lifecycle_step(self, mock_last_activity, mock_backup, mock_drop):
        mock_last_activity.return_value = None
        mock_backup.return_value = "/path/to/backup.dump"
        # db_name carries UNIQUE(db_name) on the model, so every fixture row
        # below needs its own name — one shared "acme-co" cannot represent four
        # concurrent tenant DBs.
        cluster_dbs = {"acme-partial", "acme-idle", "acme-dormant"}

        # creating -> failed (partial cleanup) after 1 day
        tdb1 = self.tdb_model.create({
            "db_name": "acme-partial", "tenant_id": "t1", "request_id": "r1", "state": "creating"
        })
        tdb1.created_at = fields.Datetime.now() - datetime.timedelta(days=2)

        self.tdb_model._lifecycle_step(tdb1, cluster_dbs, 30)
        self.assertEqual(tdb1.state, "failed")
        mock_drop.assert_called_with("acme-partial")

        # ready -> inactive after 30 days
        tdb2 = self.tdb_model.create({
            "db_name": "acme-idle", "tenant_id": "t2", "request_id": "r2", "state": "ready"
        })
        tdb2.created_at = fields.Datetime.now() - datetime.timedelta(days=31)
        tdb2.last_activity_at = False

        self.tdb_model._lifecycle_step(tdb2, cluster_dbs, 30)
        self.assertEqual(tdb2.state, "inactive")

        # inactive -> archived after 30 days
        tdb2.state_changed_at = fields.Datetime.now() - datetime.timedelta(days=31)
        self.tdb_model._lifecycle_step(tdb2, cluster_dbs, 30)
        self.assertEqual(tdb2.state, "archived")
        mock_backup.assert_called_with("acme-idle")

        # archived -> dropped after 30 days
        tdb2.state_changed_at = fields.Datetime.now() - datetime.timedelta(days=31)
        self.tdb_model._lifecycle_step(tdb2, cluster_dbs, 30)
        self.assertEqual(tdb2.state, "dropped")
        mock_drop.assert_called_with("acme-idle")
        self.assertEqual(mock_drop.call_count, 2)

        # db missing from cluster -> dropped (no _drop_tenant_db call: nothing to drop)
        tdb3 = self.tdb_model.create({
            "db_name": "missing-co", "tenant_id": "t3", "request_id": "r3", "state": "ready"
        })
        self.tdb_model._lifecycle_step(tdb3, cluster_dbs, 30)
        self.assertEqual(tdb3.state, "dropped")
        self.assertEqual(mock_drop.call_count, 2)

        # inactive -> ready (reactivation)
        tdb4 = self.tdb_model.create({
            "db_name": "acme-dormant", "tenant_id": "t4", "request_id": "r4", "state": "inactive"
        })
        tdb4.state_changed_at = fields.Datetime.now() - datetime.timedelta(days=10)
        mock_last_activity.return_value = fields.Datetime.now()

        self.tdb_model._lifecycle_step(tdb4, cluster_dbs, 30)
        self.assertEqual(tdb4.state, "ready")

    @mute_logger("odoo.sql_db")
    def test_db_name_is_unique(self):
        """UNIQUE(db_name) is the guard the provisioning path relies on."""
        from psycopg2 import IntegrityError
        self.tdb_model.create({"db_name": "dup-co", "tenant_id": "t1", "request_id": "r1"})
        with self.assertRaises(IntegrityError), self.env.cr.savepoint(flush=False):
            self.tdb_model.create({"db_name": "dup-co", "tenant_id": "t2", "request_id": "r2"})
            self.env.flush_all()
