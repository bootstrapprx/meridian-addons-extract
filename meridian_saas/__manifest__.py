{
    "name": "Meridian SaaS Provisioning",
    "version": "19.0.1.1.0",
    "category": "Poseidon/SaaS",
    "summary": "Tenant provisioning system for Meridian",
    "author": "Kodoo",
    "license": "LGPL-3",
    # qbo_bridge: the workspace roles below imply its groups, so its xmlids must
    # load first. Every profile that ships meridian_saas (usgaap, atheneum)
    # already ships the connector.
    "depends": ["base", "account", "poseidon_accounting_kernel", "qbo_bridge"],
    "data": [
        "security/meridian_security.xml",
        "security/ir.model.access.csv",
        "data/meridian_saas_cron.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
