{
    "name": "Poseidon US Tax",
    "version": "19.0.1.1.0",
    "category": "Poseidon/Tax",
    "summary": "Planning-only US tax forecast models for Poseidon",
    "author": "Kodoo",
    "license": "LGPL-3",
    "depends": ["account", "poseidon_accounting_kernel"],
    "data": [
        "security/ir.model.access.csv",
        "data/poseidon_us_tax_ruleset.xml",
        "views/poseidon_us_tax_views.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
