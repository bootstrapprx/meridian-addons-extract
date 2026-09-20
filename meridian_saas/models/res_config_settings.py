import logging
from psycopg2 import sql

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    def _remove_app_data(self, o, s=None):
        if not self._app_check_sys_op():
            raise UserError(_('Not allow.'))
            
        # Safeguard: destructive action only allowed within 45 days of company creation
        companies = self.env.companies
        now = fields.Datetime.now()
        for company in companies:
            if not company.create_date:
                continue
            delta = now - company.create_date
            if delta.days > 45:
                raise UserError(_(
                    "The workspace reset action is destructive and can only be performed "
                    "within the first 45 days of company creation. Company '%s' is too old."
                ) % company.name)
                
        cr = self.env.cr
        s = s or []
        company_ids = tuple(companies.ids)
        
        for line in o:
            try:
                if not self.env['ir.model']._get(line):
                    continue
            except Exception as e:
                _logger.warning('remove data error get ir.model: %s,%s', line, e)
                continue
                
            obj = self.pool.get(line)
            if not obj:
                t_name = line.replace('.', '_')
            else:
                t_name = obj._table

            # Dynamic check for company_id field in model
            has_company = False
            if obj is not None and 'company_id' in obj._fields:
                has_company = True
            else:
                # If no object is loaded but table exists, check schema directly
                cr.execute("""
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE table_name = %s AND column_name = 'company_id'
                """, (t_name,))
                if cr.fetchone():
                    has_company = True

            if has_company:
                try:
                    # Execute safe multi-company wipe
                    query = sql.SQL("DELETE FROM {} WHERE company_id IN %s").format(sql.Identifier(t_name))
                    cr.execute(query, (company_ids,))
                    cr.commit()
                except Exception as e:
                    _logger.warning('remove data error: %s,%s', line, e)
            else:
                _logger.warning('Skipped data wipe for %s because it lacks a company_id field. Raw delete is unsafe in shared databases.', t_name)
                
        # Reset sequences
        for line in s:
            domain = [
                ('company_id', 'in', list(company_ids)),
                '|', ('code', '=ilike', line + '%'), ('prefix', '=ilike', line + '%')
            ]
            try:
                seqs = self.env['ir.sequence'].sudo().search(domain)
                if seqs.exists():
                    seqs.write({'number_next': 1})
            except Exception as e:
                _logger.warning('reset sequence data error: %s,%s', domain, e)
                
        return True
