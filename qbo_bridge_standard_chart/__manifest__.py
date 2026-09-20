{
    "name": "QBO Bridge Master Chart of Accounts",
    "version": "19.0.1.0.1",
    "category": "Accounting/Localization",
    "summary": "Canonical master chart of accounts for QBO Bridge",
    "description": (
        "Dedicated master chart of accounts workspace for QBO Bridge. Stores "
        "the canonical chart, imports seed data, links bridge rules, and syncs "
        "selected accounts to mapped Kodoo and QBO companies."
    ),
    "author": "Kodoo",
    "website": "https://kodoo.dev",
    "depends": [
        "qbo_bridge",
    ],
    "data": [
        "security/ir.model.access.csv",
        "data/poseidon_kernel_master_chart.xml",
        "views/qbo_standard_account_views.xml",
        "views/qbo_account_bridge_rule_views.xml",
        "views/qbo_standard_chart_import_wizard_views.xml",
        "views/qbo_standard_account_sync_wizard_views.xml",
        "views/menu.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "qbo_bridge_standard_chart/static/src/scss/qbo_master_chart.scss",
        ],
    },
    "installable": True,
    "application": True,
    "auto_install": True,
    "license": "LGPL-3",
}
