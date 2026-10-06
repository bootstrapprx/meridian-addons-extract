from odoo import models


class BooksAssistant(models.Model):
    _inherit = "kodoo.ai.assistant"

    def _uses_meridian_workspace_scope(self):
        self.ensure_one()
        return self.key == "poseidon_books_agent" or super()._uses_meridian_workspace_scope()

    def _get_effective_provider(self, company_id=None):
        self.ensure_one()
        if self.key == "poseidon_books_agent" and not self.provider_id:
            workspace_agent = self.search([("key", "=", "meridian_ai_agent"), ("active", "=", True)], limit=1)
            if workspace_agent:
                return workspace_agent._get_effective_provider(company_id)
        return super()._get_effective_provider(company_id)
