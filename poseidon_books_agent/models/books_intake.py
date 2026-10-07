"""Source evidence and personal work queues; approval never posts accounting."""
import hashlib
import json
import math

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.addons.qbo_bridge.services.qbo_sync_engine import QBOSyncEngine


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str,
                                      separators=(",", ":")).encode()).hexdigest()


class BooksIntake(models.Model):
    _name = "poseidon.books.intake"
    _description = "Historical journal source evidence"
    _order = "id desc"
    _check_company_auto = True

    name = fields.Char(required=True, readonly=True)
    company_id = fields.Many2one("res.company", required=True, readonly=True, index=True)
    mapping_id = fields.Many2one("qbo.company.mapping", readonly=True, check_company=True)
    source_key = fields.Char(required=True, readonly=True, index=True)
    source_hash = fields.Char(required=True, readonly=True)
    payload_json = fields.Json(required=True, readonly=True)
    source_kind = fields.Selection([("qbo_api", "QuickBooks API"), ("qbo_report", "QuickBooks report"),
                                   ("file_journal", "Reviewed journal file")], required=True, readonly=True)
    state = fields.Selection([("received", "Received"), ("blocked", "Needs correction"),
                              ("ready", "Ready for review"), ("applied", "Draft created")], default="received", readonly=True)
    blockers_json = fields.Json(readonly=True)
    preview_json = fields.Json(readonly=True)
    preview_hash = fields.Char(readonly=True)
    move_id = fields.Many2one("account.move", readonly=True, check_company=True)
    approved_by = fields.Many2one("res.users", readonly=True)
    approved_at = fields.Datetime(readonly=True)
    _unique_revision = models.Constraint("UNIQUE(mapping_id, source_key, source_hash)", "This source revision was already received.")

    @api.model_create_multi
    def create(self, vals_list):
        raise AccessError("Historical evidence must enter through the source intake service.")

    def write(self, vals):
        raise AccessError("Source evidence and review snapshots cannot be edited directly.")

    @api.model
    def _stage_qbo(self, mapping, records, source_kind="qbo_api"):
        mapping.check_access("read")
        if mapping.company_id not in self.env.user.company_ids:
            raise AccessError("Company is outside your workspace permissions.")
        self.check_access("create")
        # Company Provenance Guard — offline resolution. A realm that does not
        # belong to this company (or is not bound to a real Intuit company yet)
        # is quarantined here: nothing is staged, so nothing can later be
        # proposed, prepared or drafted. The full live echo check runs in the
        # caller that pulls the records.
        from odoo.addons.qbo_bridge.services.qbo_provenance import (  # noqa: PLC0415
            ProvenanceResult,
            verify_realm_binding,
        )
        binding = verify_realm_binding(self.env, mapping)
        if binding is not ProvenanceResult.MATCH:
            raise ValidationError(
                "QuickBooks ownership could not be confirmed for this company. "
                "Historical intake is quarantined (%s)." % binding.value
            )
        # Serialize repeated pulls before the revision uniqueness check.
        self.env.cr.execute("SELECT id FROM qbo_company_mapping WHERE id = %s FOR UPDATE", [mapping.id])
        staged = self.browse()
        engine = QBOSyncEngine(self.env, mapping)
        groups = {}
        for record in records:
            if not isinstance(record, dict):
                raise ValidationError("A source record must be an object.")
            if engine._is_live_journal_record(record):
                key = str(record.get("Id") or "")
                if not key:
                    key = "unidentified:" + digest(record)
                groups.setdefault(("qbo_api", key, digest(record)), []).append(record)
            else:
                key = str(engine._journal_group_key(record) or "unidentified:" + digest(record))
                groups.setdefault(("qbo_report", key, None), []).append(record)
        for (kind, key, _revision), rows in groups.items():
            payload = rows[0] if kind == "qbo_api" else rows
            source_hash = digest(payload)
            item = self.search([("mapping_id", "=", mapping.id), ("source_key", "=", key), ("source_hash", "=", source_hash)], limit=1)
            if not item:
                item = super(BooksIntake, self).create({
                    "name": "Historical journal %s" % key[:120], "company_id": mapping.company_id.id,
                    "mapping_id": mapping.id, "source_key": key, "source_hash": source_hash,
                    "payload_json": payload, "source_kind": kind,
                })
            Inbox = self.env["poseidon.books.inbox"]
            if not Inbox.search_count([("intake_id", "=", item.id), ("user_id", "=", self.env.uid)]):
                Inbox.create({"intake_id": item.id})
            staged |= item
        return staged

    def _lock(self):
        self.ensure_one()
        self.check_access("write")
        if self.mapping_id:
            self.env.cr.execute("SELECT id FROM qbo_company_mapping WHERE id = %s FOR UPDATE", [self.mapping_id.id])
        else:
            self.env.cr.execute("SELECT id FROM res_company WHERE id = %s FOR UPDATE", [self.company_id.id])
        self.env.cr.execute("SELECT id FROM poseidon_books_intake WHERE id = %s FOR UPDATE", [self.id])
        self.invalidate_recordset()

    def _proposal(self):
        self.ensure_one()
        self.check_access("read")
        scoped = self.with_company(self.company_id)
        engine = QBOSyncEngine(scoped.env, scoped.mapping_id)
        blockers = []
        vals = None
        payload = self.payload_json
        if self.source_kind == "qbo_api":
            for index, line in enumerate(payload.get("Line") or []):
                detail = line.get("JournalEntryLineDetail") if isinstance(line, dict) else None
                if not isinstance(detail, dict):
                    blockers.append({"kind": "unsupported_line", "line": index})
                    continue
                amount = line.get("Amount")
                try:
                    valid = not isinstance(amount, bool) and math.isfinite(float(amount)) and float(amount) >= 0
                except (TypeError, ValueError):
                    valid = False
                if not valid or detail.get("PostingType") not in ("Debit", "Credit"):
                    blockers.append({"kind": "invalid_amount_or_side", "line": index})
                ref = detail.get("AccountRef") or {}
                # Never resolve a source account by name alone in reviewed intake.
                account = scoped.env["account.account"].search([
                    ("company_ids", "in", [self.company_id.id]), ("qbo_id", "=", str(ref.get("value") or "")),
                ], limit=2) if isinstance(ref, dict) and ref.get("value") else scoped.env["account.account"]
                decision = scoped.env["poseidon.mapping.decision"].search([
                    ("company_id", "=", self.company_id.id), ("qbo_id", "=", str(ref.get("value") or "")),
                ], limit=1) if isinstance(ref, dict) else scoped.env["poseidon.mapping.decision"]
                canonical = engine._resolve_qbo_account(ref)
                if len(account) != 1 or decision.state != "confirmed" or not canonical:
                    blockers.append({"kind": "account_mapping", "line": index, "source_account_id": ref.get("value") if isinstance(ref, dict) else None,
                                     "source_account_name": ref.get("name") if isinstance(ref, dict) else None})
                if len(account) == 1 and "qbo_source_account_type" in account._fields and account.qbo_source_account_type == "Bank" and canonical:
                    bank_journals = scoped.env["account.journal"].search([
                        ("company_id", "=", self.company_id.id), ("type", "=", "bank"),
                        ("default_account_id", "in", [account.id, canonical.id]),
                    ], limit=1)
                    if not bank_journals:
                        blockers.append({"kind": "bank_setup", "source_account_id": ref.get("value"),
                                         "source_account_name": account.name, "message": "Review bank identity and prepare a bank journal proposal."})
                if detail.get("ClassRef") and not engine._qbo_class_distribution(detail):
                    blockers.append({"kind": "cost_center", "line": index,
                                     "message": "The source QuickBooks Class has no linked cost center."})
            currency_ref = payload.get("CurrencyRef")
            if currency_ref and (not isinstance(currency_ref, dict) or currency_ref.get("value") != self.company_id.currency_id.name):
                blockers.append({"kind": "foreign_currency", "message": "Review currency conversion before creating a native draft."})
            if not payload.get("Id") or not payload.get("TxnDate"):
                blockers.append({"kind": "source_identity_or_date"})
            try:
                source_date = fields.Date.to_date(payload.get("TxnDate"))
                if not source_date:
                    raise ValueError("Missing date")
                for field in ("fiscalyear_lock_date", "tax_lock_date", "hard_lock_date", "sale_lock_date", "purchase_lock_date"):
                    locked = self.company_id[field] if field in self.company_id._fields else False
                    if locked and source_date <= locked:
                        blockers.append({"kind": "locked_period", "lock": field})
            except (ValueError, TypeError):
                blockers.append({"kind": "invalid_source_date"})
            if not blockers:
                vals = engine._build_live_journal_move_vals(payload)
        else:
            # Report parsers vary; retain ambiguous evidence instead of guessing.
            blockers.append({"kind": "report_review", "message": "Report rows need a reviewed transactional normalization."})
        if not engine._default_journal():
            blockers.append({"kind": "general_journal", "message": "Configure a general journal before approval."})
        if not vals and not blockers:
            blockers.append({"kind": "unbalanced_or_incomplete"})
        existing = scoped.env["account.move"].search([
            ("company_id", "=", self.company_id.id), ("qbo_id", "=", self.source_key),
        ], limit=1) if self.source_kind == "qbo_api" else scoped.env["account.move"]
        if existing and existing != self.move_id:
            blockers.append({"kind": "existing_canonical_entry", "move_id": existing.id})
        accounts = scoped.env["account.account"].browse([line[2]["account_id"] for line in (vals or {}).get("line_ids", [])])
        journal = scoped.env["account.journal"].browse((vals or {}).get("journal_id"))
        snapshot = {"source_hash": self.source_hash, "values": vals, "blockers": blockers,
                    "accounts": [(a.id, str(a.write_date)) for a in accounts],
                    "journal": (journal.id, str(journal.write_date)) if journal else None,
                    "company_write_date": str(self.company_id.write_date)}
        return vals, blockers, digest(snapshot)

    def action_prepare(self):
        self._lock()
        if self.state == "applied":
            return self._summary()
        vals, blockers, fingerprint = self._proposal()
        preview = None
        if vals:
            preview = {"date": vals["date"], "journal_id": vals["journal_id"], "ref": vals["ref"],
                       "lines": [{**line[2], "account_code": self.env["account.account"].with_company(self.company_id).browse(line[2]["account_id"]).code,
                                  "account_name": self.env["account.account"].with_company(self.company_id).browse(line[2]["account_id"]).name} for line in vals["line_ids"]], "company_id": self.company_id.id}
        super(BooksIntake, self).write({"state": "blocked" if blockers else "ready", "blockers_json": blockers,
                                       "preview_json": preview, "preview_hash": fingerprint})
        return self._summary()

    def action_approve_draft(self, expected_hash):
        self._lock()
        if not self.env.user.has_group("account.group_account_manager"):
            raise AccessError("An accounting manager must approve historical journal drafts.")
        if not expected_hash or expected_hash != self.preview_hash:
            raise UserError("Review the current proposal before approving.")
        if self.state == "applied":
            return self._summary()
        # Serialize approvals across different revisions of the same connection.
        if self.mapping_id:
            self.env.cr.execute("SELECT id FROM qbo_company_mapping WHERE id = %s FOR UPDATE", [self.mapping_id.id])
        vals, blockers, fingerprint = self._proposal()
        if blockers or self.state != "ready" or fingerprint != expected_hash:
            raise UserError("Source or accounting setup changed. Prepare and review again.")
        date = fields.Date.to_date(vals.get("date"))
        for field in ("fiscalyear_lock_date", "tax_lock_date", "hard_lock_date", "sale_lock_date", "purchase_lock_date"):
            locked = self.company_id[field] if field in self.company_id._fields else False
            if locked and date <= locked:
                raise UserError("Historical date is in a locked accounting period.")
        # Native create enforces domain checks. Never action_post or override locks.
        move = self.env["account.move"].with_company(self.company_id).create(vals)
        super(BooksIntake, self).write({"state": "applied", "move_id": move.id,
                                       "approved_by": self.env.uid, "approved_at": fields.Datetime.now()})
        return self._summary()

    def _summary(self):
        self.ensure_one()
        return {"id": self.id, "name": self.name, "company_id": self.company_id.id,
                "company_name": self.company_id.name, "source_key": self.source_key,
                "source_kind": self.source_kind, "state": self.state,
                "blockers": self.blockers_json or [], "preview": self.preview_json,
                "preview_hash": self.preview_hash or None, "move_id": self.move_id.id or None}

    @api.model
    def list_for_review(self, company_ids, limit=50, offset=0):
        if not isinstance(company_ids, list) or not company_ids or len(company_ids) > 10 or any(type(i) is not int or i < 1 for i in company_ids):
            raise ValidationError("Select one to ten authorized companies.")
        if not set(company_ids).issubset(self.env.user.company_ids.ids):
            raise AccessError("Company scope is not authorized.")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValidationError("Review limit must be between 1 and 100.")
        if type(offset) is not int or not 0 <= offset <= 100000:
            raise ValidationError("Review offset is invalid.")
        model = self.with_context(allowed_company_ids=company_ids)
        domain = [("company_id", "in", company_ids)]
        items = model.search(domain, limit=limit, offset=offset)
        inbox = self.env["poseidon.books.inbox"].with_context(allowed_company_ids=company_ids).search([("intake_id", "in", items.ids)])
        personal = {row.intake_id.id: {"id": row.id, "is_read": row.is_read, "folder": row.folder, "note": row.note or ""} for row in inbox}
        tasks = self.env["poseidon.books.pull"].with_context(allowed_company_ids=company_ids).search([("company_id", "in", company_ids)], limit=10)
        return {"items": [{**item._summary(), "inbox": personal.get(item.id)} for item in items],
                "tasks": [{"id": t.id, "company_id": t.company_id.id, "state": t.state, "received_count": t.received_count, "message": t.message or ""} for t in tasks],
                "total": model.search_count(domain),
                "can_approve": self.env.user.has_group("account.group_account_manager"),
                "readiness": model.historical_readiness(company_ids)}


class BooksInbox(models.Model):
    _name = "poseidon.books.inbox"
    _description = "Personal historical accounting work item"
    _order = "id desc"
    intake_id = fields.Many2one("poseidon.books.intake", required=True, readonly=True, ondelete="restrict")
    company_id = fields.Many2one(related="intake_id.company_id", store=True, readonly=True)
    user_id = fields.Many2one("res.users", required=True, readonly=True, default=lambda self: self.env.user)
    is_read = fields.Boolean(default=False)
    folder = fields.Selection([("inbox", "Inbox"), ("saved", "Saved"), ("archive", "Archive")], default="inbox", required=True)
    note = fields.Text()
    _unique_recipient = models.Constraint("UNIQUE(intake_id, user_id)", "This item is already in your inbox.")

    @api.model_create_multi
    def create(self, vals_list):
        clean = []
        for vals in vals_list:
            if set(vals) - {"intake_id"}:
                raise AccessError("Inbox ownership and initial state are assigned by the backend.")
            intake = self.env["poseidon.books.intake"].browse(vals.get("intake_id")).exists()
            intake.ensure_one()
            intake.check_access("read")
            clean.append({"intake_id": intake.id, "user_id": self.env.uid})
        return super().create(clean)

    def write(self, vals):
        if set(vals) - {"is_read", "folder", "note"}:
            raise AccessError("Inbox source and recipient cannot be changed.")
        return super().write(vals)


class BooksPull(models.Model):
    _name = "poseidon.books.pull"
    _description = "Durable historical journal intake request"
    _order = "id desc"
    mapping_id = fields.Many2one("qbo.company.mapping", required=True, readonly=True)
    company_id = fields.Many2one(related="mapping_id.company_id", store=True, readonly=True)
    user_id = fields.Many2one("res.users", required=True, readonly=True)
    state = fields.Selection([("queued", "Queued"), ("done", "Received"), ("failed", "Failed")], default="queued", readonly=True)
    received_count = fields.Integer(readonly=True)
    message = fields.Char(readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        raise AccessError("Use the historical intake request action.")

    def write(self, vals):
        raise AccessError("Historical intake task status is maintained by the worker.")

    @api.model
    def request_history(self, company_id):
        if type(company_id) is not int or company_id not in self.env.user.company_ids.ids:
            raise AccessError("Select an authorized company.")
        if not self.env.user.has_group("account.group_account_manager"):
            raise AccessError("An accounting manager must request historical intake.")
        self.check_access("create")
        Mapping = self.env["qbo.company.mapping"].with_company(self.env["res.company"].browse(company_id))
        mappings = Mapping.search([("company_id", "=", company_id)], limit=2)
        if len(mappings) != 1:
            raise UserError("Configure exactly one QuickBooks connection for this company.")
        mapping = mappings[0]
        self.env.cr.execute("SELECT id FROM qbo_company_mapping WHERE id = %s FOR UPDATE", [mapping.id])
        task = self.search([("mapping_id", "=", mapping.id), ("state", "=", "queued")], limit=1)
        if not task:
            task = super(BooksPull, self).create({"mapping_id": mapping.id, "user_id": self.env.uid})
        cron = self.env.ref("poseidon_books_agent.books_pull_cron")
        cron.sudo()._trigger()
        return {"id": task.id, "state": task.state, "company_id": company_id}

    @api.model
    def _cron_receive_history(self):
        # The system worker enumerates tasks only; domain work uses the saved requester.
        for task in self.search([("state", "=", "queued")], limit=3):
            self.env.cr.execute("SELECT id FROM poseidon_books_pull WHERE id = %s FOR UPDATE SKIP LOCKED", [task.id])
            if not self.env.cr.fetchone():
                continue
            try:
                with self.env.cr.savepoint():
                    operator = task.with_user(task.user_id).with_context(allowed_company_ids=[task.company_id.id])
                    operator.check_access("read")
                    if not operator.env.user.active or not operator.env.user.has_group("account.group_account_manager"):
                        raise AccessError("Requester can no longer run this intake.")
                    engine = QBOSyncEngine(operator.env, operator.mapping_id)
                    engine.verify_provenance()
                    records = engine.client.get_journal_entries(modified_since=None)
                    received = operator.env["poseidon.books.intake"]._stage_qbo(operator.mapping_id, records)
                    for item in received:
                        item.action_prepare()
                    super(BooksPull, task).write({"state": "done", "received_count": len(received),
                                                 "message": "History received for review; no accounting entries created."})
            except Exception:
                super(BooksPull, task).write({"state": "failed", "message": "History intake failed. Check the connection and permissions, then request again."})
