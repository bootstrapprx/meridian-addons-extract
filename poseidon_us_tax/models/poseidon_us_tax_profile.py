from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

ALL_TOOL_KEYS = (
    "aiCenter",
    "biDashboards",
    "expenses",
    "fleet",
    "invoicing",
    "lunch",
    "maintenance",
    "people",
    "project",
    "realEstate",
    "recruitment",
    "survey",
)

ACTIVITY_CATALOG = (
    {
        "value": "software",
        "label": "Software / SaaS",
        "l3_tags": ("Universal",),
        "tools": ("aiCenter", "biDashboards", "expenses", "invoicing", "people", "project", "survey"),
        "subject_to_sales_tax": True,
        "subject_to_business_tax": True,
    },
    {
        "value": "e_commerce",
        "label": "E-Commerce",
        "l3_tags": ("Universal",),
        "tools": ("aiCenter", "biDashboards", "expenses", "invoicing", "people", "project", "survey"),
        "subject_to_sales_tax": True,
        "subject_to_business_tax": True,
    },
    {
        "value": "real_estate",
        "label": "Real Estate",
        "l3_tags": ("Universal",),
        "tools": ("aiCenter", "biDashboards", "expenses", "invoicing", "maintenance", "people", "project", "realEstate"),
        "subject_to_sales_tax": False,
        "subject_to_business_tax": True,
    },
    {
        "value": "non_profit",
        "label": "Non-Profit",
        "l3_tags": ("Universal",),
        "tools": ("aiCenter", "biDashboards", "expenses", "invoicing", "people", "project", "survey"),
        "subject_to_sales_tax": False,
        "subject_to_business_tax": False,
    },
    {
        "value": "services",
        "label": "Services",
        "l3_tags": ("Services",),
        "tools": ("aiCenter", "biDashboards", "expenses", "invoicing", "people", "project", "survey"),
        "subject_to_sales_tax": False,
        "subject_to_business_tax": True,
    },
    {
        "value": "trade_retail",
        "label": "Trade/Retail",
        "l3_tags": ("Trade/Retail",),
        "tools": ("aiCenter", "biDashboards", "expenses", "invoicing", "people", "project", "survey"),
        "subject_to_sales_tax": True,
        "subject_to_business_tax": True,
    },
    {
        "value": "manufacturing",
        "label": "Manufacturing",
        "l3_tags": ("Manufacturing",),
        "tools": ("aiCenter", "biDashboards", "expenses", "invoicing", "maintenance", "people", "project"),
        "subject_to_sales_tax": True,
        "subject_to_business_tax": True,
    },
    {
        "value": "marketing_consulting",
        "label": "Marketing Consulting",
        "l3_tags": ("Services",),
        "tools": ("aiCenter", "biDashboards", "expenses", "invoicing", "people", "project", "survey"),
        "subject_to_sales_tax": False,
        "subject_to_business_tax": True,
    },
    {
        "value": "portfolio_management",
        "label": "Portfolio Management",
        "l3_tags": ("Services",),
        "tools": ("aiCenter", "biDashboards", "invoicing", "project"),
        "subject_to_sales_tax": False,
        "subject_to_business_tax": True,
    },
    {
        "value": "credit_intermediation",
        "label": "Credit Intermediation",
        "l3_tags": ("Services",),
        "tools": ("aiCenter", "biDashboards", "invoicing", "project"),
        "subject_to_sales_tax": False,
        "subject_to_business_tax": True,
    },
    {
        "value": "residential_property_management",
        "label": "Residential Property Management",
        "l3_tags": ("Universal",),
        "tools": ("aiCenter", "biDashboards", "expenses", "invoicing", "maintenance", "people", "project", "realEstate"),
        "subject_to_sales_tax": False,
        "subject_to_business_tax": True,
    },
    {
        "value": "computer_systems_design",
        "label": "Computer Systems Design",
        "l3_tags": ("Services",),
        "tools": ("aiCenter", "biDashboards", "expenses", "invoicing", "people", "project", "survey"),
        "subject_to_sales_tax": False,
        "subject_to_business_tax": True,
    },
    {
        "value": "research_development",
        "label": "Research & Development",
        "l3_tags": ("Services",),
        "tools": ("aiCenter", "biDashboards", "expenses", "invoicing", "people", "project", "survey"),
        "subject_to_sales_tax": False,
        "subject_to_business_tax": True,
    },
    {
        "value": "real_estate_other",
        "label": "Other Real Estate Activities",
        "l3_tags": ("Universal",),
        "tools": ("aiCenter", "biDashboards", "invoicing", "maintenance", "project", "realEstate"),
        "subject_to_sales_tax": False,
        "subject_to_business_tax": True,
    },
    {
        "value": "janitorial_services",
        "label": "Janitorial Services",
        "l3_tags": ("Services",),
        "tools": ("aiCenter", "biDashboards", "expenses", "invoicing", "maintenance", "people", "project"),
        "subject_to_sales_tax": False,
        "subject_to_business_tax": True,
    },
    {
        "value": "universal",
        "label": "Universal",
        "l3_tags": ("Universal",),
        "tools": ALL_TOOL_KEYS,
        "subject_to_sales_tax": False,
        "subject_to_business_tax": False,
    },
    {
        "value": "unknown",
        "label": "Unknown",
        "l3_tags": ("Universal",),
        "tools": ALL_TOOL_KEYS,
        "subject_to_sales_tax": False,
        "subject_to_business_tax": False,
    },
)

ACTIVITY_BY_KEY = {item["value"]: item for item in ACTIVITY_CATALOG}

# Default cost centers per activity, as (code, name, type).
#
# A template, not a rule: it seeds the analytic dimensions a company of this
# kind almost always needs, and the operator adds, renames or archives from
# there. Kept as its own table rather than a field on each catalog entry so the
# whole set can be read and reviewed at once — an accountant should be able to
# audit these fifteen lists without reading the tool and tax columns beside them.
#
# `type` values come from poseidon_cost_center's COST_CENTER_TYPES
# (department / property / project / shared_service / administrative). Every
# activity ends with ADM: overhead exists everywhere, and naming it explicitly
# is what keeps it OUT of the operating centers.
_ADMIN = ("ADM", "Administration", "administrative")

COST_CENTER_TEMPLATES = {
    "software": (
        ("ENG", "Engineering", "department"),
        ("PRD", "Product", "department"),
        ("SLS", "Sales & Marketing", "department"),
        ("CS", "Customer Success", "department"),
        _ADMIN,
    ),
    "e_commerce": (
        ("FUL", "Fulfillment", "department"),
        ("MKT", "Marketing", "department"),
        ("CS", "Customer Service", "department"),
        _ADMIN,
    ),
    "real_estate": (
        ("PROP", "Property Operations", "property"),
        ("LEAS", "Leasing", "department"),
        ("MNT", "Maintenance", "department"),
        _ADMIN,
    ),
    "real_estate_other": (
        ("PROP", "Property Operations", "property"),
        ("TXN", "Transactions & Closings", "project"),
        ("MNT", "Maintenance", "department"),
        _ADMIN,
    ),
    "residential_property_management": (
        ("PROP", "Property Operations", "property"),
        ("LEAS", "Leasing & Tenanting", "department"),
        ("MNT", "Maintenance & Turns", "department"),
        _ADMIN,
    ),
    "non_profit": (
        # The functional split donors and Form 990 both expect.
        ("PRG", "Programs", "department"),
        ("FND", "Fundraising", "department"),
        _ADMIN,
    ),
    "services": (
        ("DEL", "Client Delivery", "project"),
        ("BD", "Business Development", "department"),
        _ADMIN,
    ),
    "trade_retail": (
        ("STO", "Store Operations", "department"),
        ("PUR", "Purchasing", "department"),
        ("FUL", "Fulfillment", "department"),
        _ADMIN,
    ),
    "manufacturing": (
        ("PRO", "Production", "department"),
        ("QC", "Quality Control", "department"),
        ("MNT", "Maintenance", "department"),
        ("WHS", "Warehouse", "department"),
        _ADMIN,
    ),
    "marketing_consulting": (
        ("DEL", "Client Delivery", "project"),
        ("CRE", "Creative & Content", "department"),
        ("BD", "Business Development", "department"),
        _ADMIN,
    ),
    "portfolio_management": (
        ("INV", "Investment Management", "department"),
        ("RES", "Research & Analysis", "department"),
        ("CMP", "Compliance", "shared_service"),
        _ADMIN,
    ),
    "credit_intermediation": (
        ("ORG", "Origination", "department"),
        ("UW", "Underwriting", "department"),
        ("SRV", "Loan Servicing", "department"),
        ("CMP", "Compliance", "shared_service"),
        _ADMIN,
    ),
    "computer_systems_design": (
        ("ENG", "Engineering", "department"),
        ("DEL", "Client Delivery", "project"),
        ("SLS", "Sales & Marketing", "department"),
        _ADMIN,
    ),
    "research_development": (
        ("RND", "Research & Development", "project"),
        ("LAB", "Laboratory & Equipment", "shared_service"),
        ("GRT", "Grants & Contracts", "department"),
        _ADMIN,
    ),
    "janitorial_services": (
        ("FLD", "Field Operations", "department"),
        ("SUP", "Supplies & Equipment", "shared_service"),
        ("SLS", "Sales", "department"),
        _ADMIN,
    ),
}

# An unclassified company still gets a usable split rather than nothing.
DEFAULT_COST_CENTER_TEMPLATE = (
    ("OPS", "Operations", "department"),
    ("SLS", "Sales & Marketing", "department"),
    _ADMIN,
)


def cost_center_template_for(activity_tag):
    """Template rows for an activity, falling back to the generic split."""
    return COST_CENTER_TEMPLATES.get(activity_tag or "", DEFAULT_COST_CENTER_TEMPLATE)

# Convenience preselection for L3 batch activation — never a gate.
L3_ACTIVITY_TAG_MAP = {
    value: item["l3_tags"] for value, item in ACTIVITY_BY_KEY.items()
}

TOOLS_BY_ACTIVITY = {
    value: item["tools"] for value, item in ACTIVITY_BY_KEY.items()
}


class PoseidonUsTaxProfile(models.Model):
    _name = "poseidon.us.tax.profile"
    _description = "Poseidon US Tax Profile"
    _order = "company_id"

    name = fields.Char(compute="_compute_name", store=True)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        index=True,
        ondelete="cascade",
    )
    entity_type = fields.Selection(
        [
            ("llc", "LLC"),
            ("s_corp", "S-Corp"),
            ("c_corp", "C-Corp"),
            ("partnership", "Partnership"),
            ("sole_proprietor", "Sole proprietor"),
            ("unknown", "Unknown"),
        ],
        required=True,
        default="unknown",
        index=True,
    )
    activity_tag = fields.Selection(
        [(item["value"], item["label"]) for item in ACTIVITY_CATALOG],
        required=True,
        default="unknown",
    )
    subject_to_sales_tax = fields.Boolean(default=False)
    subject_to_business_tax = fields.Boolean(default=False)
    tax_regime = fields.Selection(
        [
            ("pass_through", "Pass-through"),
            ("c_corp", "C-Corp entity tax"),
            ("unknown", "Unknown"),
        ],
        required=True,
        default="unknown",
        index=True,
    )
    accounting_method = fields.Selection(
        [
            ("cash", "Cash"),
            ("accrual", "Accrual"),
            ("hybrid", "Hybrid"),
            ("unknown", "Unknown"),
        ],
        required=True,
        default="unknown",
    )
    fiscal_year_end_month = fields.Integer(required=True, default=12)
    fiscal_year_end_day = fields.Integer(required=True, default=31)
    state_code = fields.Char(string="State", size=2)
    federal_ein = fields.Char(string="Federal EIN")
    active = fields.Boolean(default=True)
    notes = fields.Text()

    _company_unique = models.Constraint(
        "UNIQUE(company_id)",
        "Only one Poseidon US tax profile is allowed per company.",
    )

    @api.depends("company_id", "entity_type", "tax_regime")
    def _compute_name(self):
        for profile in self:
            company = profile.company_id.display_name or _("Company")
            entity = dict(profile._fields["entity_type"].selection).get(profile.entity_type, profile.entity_type)
            regime = dict(profile._fields["tax_regime"].selection).get(profile.tax_regime, profile.tax_regime)
            profile.name = _("%(company)s - %(entity)s / %(regime)s") % {
                "company": company,
                "entity": entity,
                "regime": regime,
            }

    @api.constrains("fiscal_year_end_month", "fiscal_year_end_day")
    def _check_fiscal_year_end(self):
        for profile in self:
            if profile.fiscal_year_end_month < 1 or profile.fiscal_year_end_month > 12:
                raise ValidationError(_("Fiscal year end month must be between 1 and 12."))
            if profile.fiscal_year_end_day < 1 or profile.fiscal_year_end_day > 31:
                raise ValidationError(_("Fiscal year end day must be between 1 and 31."))

    @api.constrains("state_code")
    def _check_state_code(self):
        for profile in self:
            if profile.state_code and len(profile.state_code.strip()) != 2:
                raise ValidationError(_("State must be a two-letter code."))

    @api.model
    def l3_activity_tag_map(self):
        """Full profile activity_tag -> L3 activity tags mapping."""
        return dict(L3_ACTIVITY_TAG_MAP)

    @api.model
    def activity_catalog(self):
        """Meridian activity catalog: labels, L3 preselection and tool access."""
        return [
            {
                "value": item["value"],
                "label": item["label"],
                "l3_tags": list(item["l3_tags"]),
                "tools": list(item["tools"]),
                "subject_to_sales_tax": item["subject_to_sales_tax"],
                "subject_to_business_tax": item["subject_to_business_tax"],
                "cost_centers": [
                    {"code": code, "name": name, "type": center_type}
                    for code, name, center_type in cost_center_template_for(item["value"])
                ],
            }
            for item in ACTIVITY_CATALOG
        ]

    @api.model
    def cost_center_templates(self):
        """activity_tag -> default cost centers, for previewing before seeding."""
        return {
            value: [
                {"code": code, "name": name, "type": center_type}
                for code, name, center_type in cost_center_template_for(value)
            ]
            for value in ACTIVITY_BY_KEY
        }

    def poseidon_cost_center_template(self):
        """The template this company's activity implies. One row per center."""
        self.ensure_one()
        return [
            {"code": code, "name": name, "type": center_type}
            for code, name, center_type in cost_center_template_for(self.activity_tag)
        ]

    @api.model
    def activity_tools(self):
        """Profile activity_tag -> operational tool keys."""
        return {
            value: list(tools) for value, tools in TOOLS_BY_ACTIVITY.items()
        }

    def poseidon_l3_activity_tags(self):
        """L3 activity tags preselected for this profile's activity_tag.

        Convenience preselection for L3 batch activation; never a gate.
        """
        mapped = L3_ACTIVITY_TAG_MAP.get(self.activity_tag)
        return list(mapped) if mapped else ["Universal"]

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("state_code"):
                vals["state_code"] = vals["state_code"].strip().upper()
        return super().create(vals_list)

    def write(self, vals):
        vals = dict(vals)
        if vals.get("state_code"):
            vals["state_code"] = vals["state_code"].strip().upper()
        return super().write(vals)
