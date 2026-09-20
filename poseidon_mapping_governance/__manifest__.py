{
    "name": "Poseidon Mapping Governance",
    "version": "19.0.1.0.0",
    "category": "Poseidon/Accounting",
    "summary": "Human-reviewed mapping propagation across Poseidon company groups",
    "author": "Kodoo",
    "license": "LGPL-3",
    "depends": [
        "account",
        "mail",
        "qbo_bridge_standard_chart",
        "poseidon_accounting_kernel",
        "poseidon_company_group",
    ],
    "data": [
        "security/ir.model.access.csv",
        "views/poseidon_mapping_governance_views.xml",
        "wizard/poseidon_propagation_wizard_views.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
