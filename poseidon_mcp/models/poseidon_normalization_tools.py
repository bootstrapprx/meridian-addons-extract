"""Read-only normalization evidence for an accountant-reviewed local ledger."""
from difflib import SequenceMatcher

from odoo import api, models
from odoo.exceptions import ValidationError

from .poseidon_planning_tools import positive_id


class PoseidonNormalizationTools(models.AbstractModel):
    _inherit = "poseidon.mcp.tools"

    def _execute_historical_normalization_review(self, job, payload):
        del job
        company = self._planning_company(positive_id(payload.get("company_id")))
        company = company.with_context(allowed_company_ids=[company.id])
        offset = payload.get("offset", 0)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValidationError(self.env._("Choose a valid source offset."))
        accounts = self.env["account.account"].with_context(allowed_company_ids=[company.id])
        canonical = accounts.search([
            ("company_ids", "in", company.ids), ("active", "=", True),
            ("poseidon_kernel_layer", "=", "L3"), ("qbo_id", "=", False), ("qbo_source_name", "=", False),
        ], order="code, id", limit=1001)
        source_domain = [("company_ids", "in", company.ids), "|",
                         ("qbo_id", "!=", False), ("qbo_source_name", "!=", False)]
        sources = accounts.with_context(active_test=False).search(source_domain, offset=offset, limit=25, order="id")
        master_model = self.env["qbo.standard.account"]
        master_access = master_model.has_access("read")
        masters = master_model.search([("active", "=", True), ("entry_type", "=", "detail"), ("kernel_layer", "=", "L3")], order="code, id", limit=1001) if master_access else master_model.browse()
        active_by_master = {account.qbo_standard_account_id.id: account for account in canonical if account.qbo_standard_account_id}
        options = [{"account_id": account.id, "standard_id": account.qbo_standard_account_id.id or None, "code": account.code,
                    "name": account.name, "type": account.account_type, "activation_required": False,
                    "reference": "account.account:%s" % account.id} for account in canonical]
        options += [{"account_id": None, "standard_id": master.id, "code": master.code, "name": master.description,
                     "type": master.odoo_account_type, "activation_required": True,
                     "reference": "qbo.standard.account:%s" % master.id} for master in masters[:1000] if master.id not in active_by_master]
        decisions = self.env["poseidon.mapping.decision"].with_context(allowed_company_ids=[company.id]).search([
            ("company_id", "=", company.id), ("qbo_id", "in", sources.mapped("qbo_id")),
        ]) if "poseidon.mapping.decision" in self.env else None
        rows = []
        for source in sources:
            same_type = [option for option in options if option["type"] == source.account_type]
            candidates = sorted(same_type, key=lambda option: SequenceMatcher(
                None, (source.qbo_source_name or source.name).casefold(), option["name"].casefold(),
            ).ratio(), reverse=True)[:3]
            existing = decisions.filtered(lambda decision: decision.qbo_id == source.qbo_id) if decisions is not None else []
            sample = self.env["account.move.line"].with_context(allowed_company_ids=[company.id]).search([
                ("company_id", "=", company.id), ("account_id", "=", source.id),
            ], order="date desc, id desc", limit=5)
            rows.append({
                "source_reference": "account.account:%s" % source.id,
                "source_id": source.id, "source_code": source.code,
                "source_name": source.qbo_source_name or source.name,
                "source_type": source.account_type, "qbo_id": source.qbo_id,
                "historical_samples": [{"reference": "account.move.line:%s" % line.id,
                    "document_reference": "account.move:%s" % line.move_id.id,
                    "document_type": line.move_id.move_type,
                    "date": str(line.date), "memo": (line.name or "")[:500],
                    "debit": line.debit, "credit": line.credit, "state": line.parent_state,
                } for line in sample],
                "candidates": [{**option,
                    "reason": "Same accounting type; name similarity only, not an accounting conclusion.",
                    "name_similarity": round(SequenceMatcher(None, (source.qbo_source_name or source.name).casefold(), option["name"].casefold()).ratio(), 3),
                } for option in candidates],
                "existing_mappings": [{"id": decision.id, "state": decision.state,
                    "destination_account_id": decision.destination_account_id.id or None,
                    "destination_name": decision.destination_account_id.name or "",
                    "destination_is_active_l3": bool(decision.destination_account_id.active and decision.destination_account_id.poseidon_kernel_layer == "L3" and not decision.destination_account_id.qbo_id and not decision.destination_account_id.qbo_source_name),
                } for decision in existing],
                "review_flags": ["Customer invoice credited to an asset/clearing source; inspect each invoice line before mapping the entire source."] if source.account_type in ("asset_current", "asset_cash") and any(line.credit and line.move_id.move_type == "out_invoice" for line in sample) else [],
                "review_required": True,
            })
        return {"company_id": company.id, "company_name": company.name,
                "chart_identity": "kernel_l3", "kernel_ready": bool(canonical),
                "blocked": not bool(canonical), "canonical_catalog_truncated": len(canonical) > 1000 or len(masters) > 1000,
                "activation_catalog_available": master_access,
                "available_l3_catalog": [{"standard_id": master.id, "code": master.code, "name": master.description, "type": master.odoo_account_type} for master in masters[:1000]],
                "canonical_accounts": [{"id": account.id, "code": account.code, "name": account.name,
                    "type": account.account_type, "reference": "account.account:%s" % account.id,
                } for account in canonical[:1000]],
                "offset": offset, "total_sources": accounts.with_context(active_test=False).search_count(source_domain),
                "sources": rows, "applied": False, "qbo_writes": False,
                "policy": "Preparation only. QBO operations are read-only; qbo.standard.account is local Kernel master storage, not the external QBO chart. Evaluate memo, counterpart accounts, opening balances and historical coverage before proposing mappings. Name similarity is not confidence. Confirm local destinations before draft creation; posting requires separate accountant approval. Never post normalized entries back to QBO."}

    @api.model
    def _get_mcp_operation_catalog(self):
        catalog = super()._get_mcp_operation_catalog()
        key = "poseidon.historical_normalization_review"
        catalog[key] = {"key": key, "version": 1, "label": "Historical normalization review",
            "category": "poseidon", "bundle": "poseidon",
            "description": "Prepare paged QBO source evidence and active Kernel L3 destination candidates for accountant review. Read-only; no QBO writes, mapping confirmation or posting.",
            "available": "poseidon_kernel_layer" in self.env["account.account"]._fields,
            "heavy": False, "supports_process_now": True,
            "requires_modules": ["poseidon_accounting_kernel"],
            "payload_outline": {"required": ["company_id"], "optional": ["offset"]},
            "result_outline": {"result": "Company-scoped cited review packet, readiness and candidate evidence."}}
        return catalog


class PoseidonNormalizationJob(models.Model):
    _inherit = "kodoo.mcp.job"

    def _execute_operation(self, user, payload):
        if self.operation_key == "poseidon.historical_normalization_review":
            return self.env["poseidon.mcp.tools"].with_user(user)._execute_historical_normalization_review(self, payload)
        return super()._execute_operation(user, payload)

    @api.model
    def _json_schema_from_payload_outline(self, descriptor):
        if descriptor["key"] != "poseidon.historical_normalization_review":
            return super()._json_schema_from_payload_outline(descriptor)
        return {"type": "object", "properties": {
            "company_id": {"type": "integer", "minimum": 1},
            "offset": {"type": "integer", "minimum": 0},
        }, "required": ["company_id"], "additionalProperties": False}
