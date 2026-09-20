from odoo import api, models, fields

class MeridianSaasTenantDb(models.Model):
    _name = "meridian.saas.tenant_db"
    _description = "Meridian dedicated tenant database"

    db_name = fields.Char(required=True, index=True)
    tenant_id = fields.Char(required=True, index=True)
    request_id = fields.Char(index=True)
    state = fields.Selection(
        [("creating", "Creating"), ("ready", "Ready"), ("inactive", "Inactive"),
         ("archived", "Archived"), ("dropped", "Dropped"), ("failed", "Failed")],
        default="creating", required=True)
    last_activity_at = fields.Datetime()
    state_changed_at = fields.Datetime(default=fields.Datetime.now)
    archived_at = fields.Datetime()
    dropped_at = fields.Datetime()
    backup_path = fields.Char()
    message = fields.Text()
    created_at = fields.Datetime(default=fields.Datetime.now, required=True)

    _db_name_uniq = models.Constraint("UNIQUE(db_name)", "Tenant database name must be unique.")

    @api.model
    def _cron_tenant_db_lifecycle(self):
        import odoo
        from odoo.tools.config import config
        from odoo.modules.registry import Registry
        from psycopg2.sql import SQL
        import os
        import shutil
        import subprocess
        from odoo.tools.misc import find_pg_tool, exec_pg_environ
        from contextlib import closing
        import logging
        
        _logger = logging.getLogger(__name__)

        LIFECYCLE_DAYS = 30
        
        with closing(odoo.sql_db.db_connect("postgres").cursor()) as cr:
            cr.execute("SELECT datname FROM pg_database WHERE NOT datistemplate")
            cluster_dbs = {row[0] for row in cr.fetchall()}

        for tdb in self.search([]):
            try:
                self._lifecycle_step(tdb, cluster_dbs, LIFECYCLE_DAYS)
                self.env.cr.commit()
            except Exception as e:
                _logger.error(f"Error in tenant DB lifecycle for {tdb.db_name}: {e}")
                self.env.cr.rollback()

    def _lifecycle_step(self, tdb, cluster_dbs, lifecycle_days):
        import datetime
        from odoo import fields

        if tdb.db_name not in cluster_dbs and tdb.state != "dropped":
            tdb.write({"state": "dropped", "dropped_at": fields.Datetime.now()})
            return

        if tdb.state == "creating":
            if (fields.Datetime.now() - tdb.created_at).days >= 1:
                self._drop_tenant_db(tdb.db_name)
                tdb.write({"state": "failed", "message": "Partial database cleaned up."})
            return

        if tdb.state in ("ready", "inactive", "archived"):
            last_activity = self._tenant_last_activity(tdb.db_name)
            if last_activity:
                tdb.write({"last_activity_at": last_activity})
                if tdb.state in ("inactive", "archived") and last_activity > tdb.state_changed_at:
                    tdb.write({"state": "ready", "state_changed_at": fields.Datetime.now()})
                    return

        if tdb.state == "ready":
            last_activity = tdb.last_activity_at or tdb.created_at
            if (fields.Datetime.now() - last_activity).days >= lifecycle_days:
                tdb.write({"state": "inactive", "state_changed_at": fields.Datetime.now()})
        elif tdb.state == "inactive":
            if (fields.Datetime.now() - tdb.state_changed_at).days >= lifecycle_days:
                backup_path = self._backup_tenant_db(tdb.db_name)
                tdb.write({
                    "state": "archived", 
                    "archived_at": fields.Datetime.now(),
                    "state_changed_at": fields.Datetime.now(),
                    "backup_path": backup_path
                })
        elif tdb.state == "archived":
            if (fields.Datetime.now() - tdb.state_changed_at).days >= lifecycle_days:
                self._drop_tenant_db(tdb.db_name)
                tdb.write({
                    "state": "dropped",
                    "dropped_at": fields.Datetime.now(),
                    "state_changed_at": fields.Datetime.now()
                })

    def _tenant_last_activity(self, db_name):
        import odoo
        from contextlib import closing
        try:
            with closing(odoo.sql_db.db_connect(db_name).cursor()) as cr:
                cr.execute("""SELECT GREATEST(
                                (SELECT max(last_activity) FROM res_device_log),
                                (SELECT max(login_date)    FROM res_users),
                                (SELECT max(create_date)   FROM res_users_log))""")
                return cr.fetchone()[0]
        except Exception:
            return None

    def _backup_tenant_db(self, db_name):
        from odoo import fields
        import os
        import shutil
        import subprocess
        from odoo.tools.config import config
        from odoo.tools.misc import find_pg_tool, exec_pg_environ

        backup_dir = os.path.join(config["data_dir"], "saas_backups")
        os.makedirs(backup_dir, exist_ok=True)
        path = os.path.join(backup_dir, f"{db_name}-{fields.Datetime.now():%Y%m%d%H%M%S}.dump")
        subprocess.run([find_pg_tool("pg_dump"), "--no-owner", "--format=c",
                        f"--file={path}", db_name], env=exec_pg_environ(), check=True)
        fs = config.filestore(db_name)
        if os.path.exists(fs):
            shutil.copytree(fs, path + ".filestore", dirs_exist_ok=True)
        return path

    def _drop_tenant_db(self, db_name):
        import odoo
        from odoo.tools import SQL
        from odoo.modules.registry import Registry
        from odoo.service.db import _drop_conn, database_identifier
        from odoo.tools.config import config
        from contextlib import closing
        import os
        import shutil

        Registry.delete(db_name)
        odoo.sql_db.close_db(db_name)
        with closing(odoo.sql_db.db_connect("postgres").cursor()) as cr:
            cr._cnx.autocommit = True
            _drop_conn(cr, db_name)
            # Odoo's own SQL wrapper: %s placeholder with the identifier as a
            # constructor arg (see odoo/service/db.py). psycopg2.sql.SQL.format
            # does NOT apply here — it uses {} placeholders and would emit a
            # literal "%s", breaking the drop.
            cr.execute(SQL("DROP DATABASE %s", database_identifier(cr, db_name)))
        fs = config.filestore(db_name)
        if os.path.exists(fs):
            shutil.rmtree(fs)
