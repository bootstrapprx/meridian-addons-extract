{
    "name": "Poseidon Reconciliation",
    "version": "19.0.1.0.0",
    "category": "Poseidon/Accounting",
    "summary": "Human-reviewed standalone bank statement reconciliation for Poseidon",
    "author": "Kodoo",
    "license": "LGPL-3",
    "depends": ["account", "poseidon_accounting_kernel"],
    "data": [
        "security/ir.model.access.csv",
        "views/poseidon_reconciliation_views.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
