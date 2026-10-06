from odoo import api, models


class BooksTools(models.AbstractModel):
    _inherit = "poseidon.mcp.tools"

    @api.model
    def _get_mcp_operation_catalog(self):
        catalog = super()._get_mcp_operation_catalog()
        catalog["poseidon.books_worklist"] = {
            "key": "poseidon.books_worklist", "version": 1, "label": "Historical books worklist",
            "category": "poseidon", "bundle": "poseidon", "available": True, "heavy": False,
            "supports_process_now": True, "requires_modules": ["poseidon_books_agent"],
            "description": "Read historical journal intake, unresolved source account references, exact review previews and personal inbox state across selected companies. Intake is not accounting. Never creates, approves or posts entries.",
            "payload_outline": {"required": ["company_ids"], "optional": ["limit"]},
            "result_outline": {"result": "Historical intake and durable task progress with canonical references."},
        }
        return catalog


class BooksJob(models.Model):
    _inherit = "kodoo.mcp.job"

    def _execute_operation(self, user, payload):
        if self.operation_key == "poseidon.books_worklist":
            result = self.env["poseidon.books.intake"].with_user(user).list_for_review(payload.get("company_ids"), payload.get("limit", 20))
            # Do not export user notes or full transaction previews to the provider.
            for item in result["items"]:
                item.pop("preview", None)
                item.pop("preview_hash", None)
                item.pop("inbox", None)
                item["reference"] = "poseidon.books.intake:%s" % item["id"]
            result["policy"] = "Intake evidence only; no ledger balances or postings. Full previews remain in human review."
            return result
        return super()._execute_operation(user, payload)

    @api.model
    def _json_schema_from_payload_outline(self, descriptor):
        if descriptor["key"] != "poseidon.books_worklist":
            return super()._json_schema_from_payload_outline(descriptor)
        return {"type": "object", "properties": {
            "company_ids": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 1, "maxItems": 10, "uniqueItems": True},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
            "required": ["company_ids"], "additionalProperties": False}
