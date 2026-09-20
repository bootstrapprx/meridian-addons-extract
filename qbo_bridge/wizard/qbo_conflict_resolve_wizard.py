import json

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services.qbo_sync_engine import QBOSyncEngine


class QboConflictResolveWizard(models.TransientModel):
    """Presents both the Odoo and QBO versions of a conflicted record
    and allows the user to choose which side wins.
    """

    _name = "qbo.conflict.resolve.wizard"
    _description = "QBO conflict resolution wizard"

    conflict_id = fields.Many2one(
        "qbo.conflict", string="Conflict", required=True, ondelete="cascade",
    )

    entity_type = fields.Selection(related="conflict_id.entity_type", readonly=True)
    qbo_id = fields.Char(related="conflict_id.qbo_id", readonly=True, string="QBO ID")
    odoo_record_ref = fields.Char(
        compute="_compute_odoo_record_ref", string="Odoo record",
    )

    # ── Formatted snapshots for display ───────────────────────────────────────
    odoo_preview = fields.Text(
        compute="_compute_previews", string="Odoo version", readonly=True,
    )
    qbo_preview = fields.Text(
        compute="_compute_previews", string="QBO version", readonly=True,
    )

    @api.depends("conflict_id")
    def _compute_odoo_record_ref(self):
        for rec in self:
            c = rec.conflict_id
            if c.odoo_model and c.odoo_record_id:
                try:
                    obj = self.env[c.odoo_model].browse(c.odoo_record_id)
                    rec.odoo_record_ref = obj.display_name if obj.exists() else str(c.odoo_record_id)
                except Exception:
                    rec.odoo_record_ref = str(c.odoo_record_id)
            else:
                rec.odoo_record_ref = "—"

    @api.depends("conflict_id")
    def _compute_previews(self):
        for rec in self:
            try:
                odoo_data = json.loads(rec.conflict_id.odoo_data or "{}")
                qbo_data = json.loads(rec.conflict_id.qbo_data or "{}")
                rec.odoo_preview = "\n".join(f"{k}: {v}" for k, v in odoo_data.items())
                rec.qbo_preview = "\n".join(f"{k}: {v}" for k, v in qbo_data.items())
            except Exception:
                rec.odoo_preview = rec.conflict_id.odoo_data or ""
                rec.qbo_preview = rec.conflict_id.qbo_data or ""

    # ── Resolution choice ─────────────────────────────────────────────────────
    #
    # The MVP is pull-only. "Keep QBO version" and "Skip" are both safe (they
    # either pull QBO into Odoo or leave everything untouched). "Keep Odoo
    # version" would require writing back to QuickBooks, which the MVP does not
    # do — it is offered but blocked in action_resolve() so the choice stays
    # honest rather than silently doing nothing.
    resolution = fields.Selection(
        [
            ("resolved_qbo", "Keep QBO version — pull into Odoo"),
            ("skipped", "Skip — leave both sides unchanged"),
            ("resolved_odoo", "Keep Odoo version (unavailable in MVP — QBO write-back disabled)"),
        ],
        string="Resolution",
        required=True,
        default="resolved_qbo",
    )
    notes = fields.Text(string="Notes")

    # ── Action ────────────────────────────────────────────────────────────────

    def action_resolve(self):
        self.ensure_one()
        conflict = self.conflict_id
        if conflict.status != "pending":
            raise UserError(_("This conflict has already been resolved."))

        if self.resolution == "resolved_odoo":
            # MVP is pull-only — keeping the Odoo version would require writing
            # back to QuickBooks, which we do not do. Block it explicitly rather
            # than marking the conflict resolved while changing nothing in QBO.
            raise UserError(_(
                "Keeping the Odoo version is unavailable in the pull-only MVP "
                "because it would require writing back to QuickBooks. Choose "
                "“Keep QBO version” to pull QuickBooks into Odoo, or “Skip” to "
                "leave both sides unchanged.",
            ))

        if self.resolution == "resolved_qbo":
            self._apply_qbo_version(conflict)
        # "skipped" needs no data write

        conflict._mark_resolved(self.resolution, self.notes)
        return {"type": "ir.actions.act_window_close"}

    def _apply_qbo_version(self, conflict):
        """Re-run pull for this single record using the stored QBO snapshot."""
        engine = QBOSyncEngine(self.env, conflict.mapping_id)
        try:
            qbo_data = json.loads(conflict.qbo_data or "{}")
            qbo_data["Id"] = conflict.qbo_id
            qbo_data["_source"] = "conflict_resolve"
            engine.sync_from_file([qbo_data], conflict.entity_type)
        except Exception as exc:
            raise UserError(_("Could not apply QBO version: %s") % exc) from exc

    def _apply_odoo_version(self, conflict):
        """Dormant: keep the Odoo record and push it back to QuickBooks.

        Unavailable in the pull-only MVP. The scheduled sync no longer pushes to
        QBO, so this cannot silently happen "on the next sync" — callers are
        blocked in action_resolve(). Kept as a future implementation surface."""
        raise UserError(_(
            "Writing the Odoo version back to QuickBooks is not available in "
            "the pull-only MVP.",
        ))
