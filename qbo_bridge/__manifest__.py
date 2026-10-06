{
    'name': 'QBO Bridge',
    'version': '19.0.1.3.0',
    'category': 'Accounting/Localization',
    'summary': 'Pull-only bridge from QuickBooks Online into Kodoo (MVP)',
    'description': (
        'Pull-only synchronization from QuickBooks Online into Kodoo. The MVP '
        'reads QBO data into Odoo and never writes back to QuickBooks. '
        'Supports OAuth connections, file imports, multi-company mappings, '
        'accounting entities, conflict review, audit logs, and scheduled pulls.'
    ),
    'author': 'Kodoo',
    'website': 'https://kodoo.dev',
    'depends': [
        'account',
        'sale_management',
        'purchase',
        'product',
        'base_setup',
        'kodoo_legal',
    ],
    'data': [
        'security/qbo_security.xml',
        'security/ir.model.access.csv',
        'data/qbo_cron.xml',
        'views/qbo_tax_mapping_views.xml',
        'views/qbo_realm_views.xml',
        'views/qbo_company_mapping_views.xml',
        'views/qbo_account_bridge_rule_views.xml',
        'views/qbo_conflict_views.xml',
        'views/qbo_sync_log_views.xml',
        'views/qbo_import_wizard_views.xml',
        'views/qbo_conflict_resolve_wizard_views.xml',
        'views/qbo_settings_views.xml',
        'views/menu.xml',
    ],
    'external_dependencies': {
        'python': ['requests', 'openpyxl'],
    },
    'installable': True,
    'application': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
