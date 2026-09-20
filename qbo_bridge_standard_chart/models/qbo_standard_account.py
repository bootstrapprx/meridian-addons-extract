import csv
import hashlib
import io
import json
from pathlib import Path

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.modules.module import get_module_path

# Current frozen kernel for the US GAAP route. The backend consumes this exact
# artifact and refuses anything else: normative drift is blocking, not advisory.
# Source of truth: docs/reference/accounting/kernel_v2025.3.json.
# (Moved there on 2026-08-14; docs/accounting/ and docs/odoo/ are still accepted
# as fallbacks so an older checkout keeps working.)
CURRENT_KERNEL_VERSION = "2025.3"
CURRENT_KERNEL_FILENAME = "kernel_v2025.3.json"
# Deployments where docs/ is not co-located with the addon can point the
# backend at the kernel explicitly via this system parameter (absolute path).
KERNEL_PATH_CONFIG_PARAM = "poseidon.kernel.json_path"

# Directories searched, in order, when walking up from the addon towards the
# repository root. Keep the current location first; the rest are legacy layouts.
KERNEL_DOC_DIRS = (
    ("docs", "reference", "accounting"),
    ("docs", "accounting"),
    ("docs", "odoo"),
)

# L3 analytic derived kernel: an optional expansion of the frozen L0/L1 kernel
# into a posting-level chart. Its accounts live in the master chart but are
# NEVER mass-published; activation is on demand (individual or batch) and always
# reversible. The artifact ships under docs/reference/accounting/ (see PLAN Fase 1).
# Source of truth: docs/reference/accounting/kernel_v2025_3_L3_derived.json.
L3_KERNEL_VERSION = "2025.3-L3.1"
L3_KERNEL_FILENAME = "kernel_v2025_3_L3_derived.json"
L3_KERNEL_PATH_CONFIG_PARAM = "poseidon.kernel.l3_json_path"
# Bumped whenever the artifact's CONTENT changes without its version changing.
# Databases stamped with an older value re-import once (see
# _ensure_l3_master_chart_imported); without a bump the self-heal short-circuits
# and the DRAFT-era rows survive.
L3_KERNEL_ENRICHMENT_VERSION = "2025.3-L3.1-frozen"
L3_KERNEL_ENRICHMENT_PARAM = "poseidon.kernel.l3_enrichment_version"
L3_ACTIVITY_TAG_TOKEN_MAP = {
    "Universal": "Universal",
    "Services": "Services",
    "Trade": "Trade/Retail",
    "Retail": "Trade/Retail",
    "Manufacturing": "Manufacturing",
}

NORMAL_BALANCE_SELECTION = [
    ("debit", "Debit"),
    ("credit", "Credit"),
]

ENTRY_TYPE_SELECTION = [
    ("header", "Header"),
    ("detail", "Detail"),
]

CATEGORY_COLOR = {
    "Asset": 4,
    "Liability": 1,
    "Equity": 10,
    "Revenue": 6,
    "Cost of Goods Sold": 2,
    "Expense": 3,
    "Other": 0,
}

KERNEL_CATEGORY_LABELS = {
    "ASSET": "Asset",
    "LIABILITY": "Liability",
    "EQUITY": "Equity",
    "REVENUE": "Revenue",
    "COST_OF_GOODS_SOLD": "Cost of Goods Sold",
    "EXPENSE": "Expense",
}

KERNEL_FS_MAPPING = {
    "ASSET": "Balance Sheet",
    "LIABILITY": "Balance Sheet",
    "EQUITY": "Balance Sheet",
    "REVENUE": "Income Statement",
    "COST_OF_GOODS_SOLD": "Income Statement",
    "EXPENSE": "Income Statement",
}

KERNEL_NORMAL_BALANCE = {
    "ASSET": "debit",
    "LIABILITY": "credit",
    "EQUITY": "credit",
    "REVENUE": "credit",
    "COST_OF_GOODS_SOLD": "debit",
    "EXPENSE": "debit",
}

KERNEL_LAYER_SELECTION = [
    ("L0", "L0"),
    ("L1", "L1"),
    ("L2", "L2"),
    ("L3", "L3"),
]


class QboStandardAccount(models.Model):
    _name = "qbo.standard.account"
    _description = "QBO master chart account"
    _order = "code, entry_type desc, id"
    _rec_name = "name"

    name = fields.Char(compute="_compute_name", store=True)
    active = fields.Boolean(default=True)
    code = fields.Char(required=True, index=True)
    description = fields.Char(required=True)
    long_description = fields.Text()
    entry_type = fields.Selection(ENTRY_TYPE_SELECTION, required=True, index=True)
    category = fields.Char(required=True, index=True)
    fs_mapping = fields.Char(string="Financial statement mapping")
    parent_code = fields.Char(index=True)
    parent_id = fields.Many2one(
        "qbo.standard.account",
        string="Parent master account",
        ondelete="set null",
    )
    child_ids = fields.One2many("qbo.standard.account", "parent_id", string="Child accounts")
    child_count = fields.Integer(compute="_compute_child_count")
    normal_balance = fields.Selection(NORMAL_BALANCE_SELECTION, required=True)
    tags = fields.Text()
    default_vendors = fields.Text()
    regulatory_mapping = fields.Text()
    start_date = fields.Date()
    end_date = fields.Date()
    notes = fields.Text()
    subcategory = fields.Char()
    cash_flow_classification = fields.Char()
    cost_center = fields.Char()
    gaap_classification = fields.Char(string="GAAP classification")
    detailed_description = fields.Text()
    kernel_version = fields.Char(index=True, copy=False)
    kernel_layer = fields.Selection(
        KERNEL_LAYER_SELECTION,
        index=True,
        copy=False,
    )
    kernel_required = fields.Boolean(copy=False)
    activity_tag = fields.Char(
        string="L3 activity tag",
        index=True,
        copy=False,
        help="Business activity this L3 analytic account applies to (Universal, "
        "Services, Trade/Retail, Manufacturing or a combination). Preselection "
        "only — never a gate.",
    )
    functional_group = fields.Char(
        string="L3 functional group",
        index=True,
        copy=False,
        help="Functional grouping for L3 analytic accounts (e.g. Payroll, "
        "Inventory, G&A).",
    )
    rollup_to_l1_parent = fields.Char(
        string="L3 rollup L1 parent",
        index=True,
        copy=False,
        help="Code of the L0/L1 account this L3 account rolls up into.",
    )
    analytic = fields.Boolean(
        string="L3 analytic",
        copy=False,
        help="True for L3 analytic expansion accounts; False for preserved L0/L1 "
        "accounts carried in the L3 kernel artifact.",
    )
    dashboard_account_sets = fields.Char(
        string="L3 dashboard account sets",
        copy=False,
        help="Comma-separated dashboard account sets this L3 account belongs to "
        "(Acash, Arev, Aref, Acogs, Aopex, Apay, Adep, ACA, ACL).",
    )
    master_account_id = fields.Char(
        string="Kernel master account UUID",
        index=True,
        copy=False,
    )
    odoo_account_type = fields.Selection(
        selection=lambda self: self.env["account.account"]._fields["account_type"].selection,
        string="Odoo account type",
        required=True,
    )
    bridge_rule_ids = fields.One2many(
        "qbo.account.bridge.rule",
        "standard_account_id",
        string="Bridge rules",
    )
    bridge_rule_count = fields.Integer(compute="_compute_bridge_rule_count")
    linked_account_ids = fields.One2many(
        "account.account",
        "qbo_standard_account_id",
        string="Company accounts",
    )
    linked_account_count = fields.Integer(compute="_compute_linked_account_count")
    journal_item_count = fields.Integer(compute="_compute_journal_item_count")
    color = fields.Integer(compute="_compute_color")
    category_slug = fields.Char(compute="_compute_category_slug")

    _qbo_standard_account_code_type_uniq = models.Constraint(
        "UNIQUE(code, entry_type)",
        "Each master chart code can appear only once per entry type.",
    )

    @api.depends("code", "description", "entry_type")
    def _compute_name(self):
        for rec in self:
            label = rec.description or ""
            suffix = "Header" if rec.entry_type == "header" else "Detail"
            rec.name = f"[{rec.code}] {label} ({suffix})"

    def _compute_bridge_rule_count(self):
        grouped = self.env["qbo.account.bridge.rule"].read_group(
            [("standard_account_id", "in", self.ids)],
            ["standard_account_id"],
            ["standard_account_id"],
        )
        counts = {
            item["standard_account_id"][0]: item["standard_account_id_count"]
            for item in grouped
            if item.get("standard_account_id")
        }
        for rec in self:
            rec.bridge_rule_count = counts.get(rec.id, 0)

    def _compute_linked_account_count(self):
        grouped = self.env["account.account"].read_group(
            [("qbo_standard_account_id", "in", self.ids)],
            ["qbo_standard_account_id"],
            ["qbo_standard_account_id"],
        )
        counts = {
            item["qbo_standard_account_id"][0]: item["qbo_standard_account_id_count"]
            for item in grouped
            if item.get("qbo_standard_account_id")
        }
        for rec in self:
            rec.linked_account_count = counts.get(rec.id, 0)

    def _compute_child_count(self):
        grouped = self.read_group(
            [("parent_id", "in", self.ids)],
            ["parent_id"],
            ["parent_id"],
        )
        counts = {
            item["parent_id"][0]: item["parent_id_count"]
            for item in grouped
            if item.get("parent_id")
        }
        for rec in self:
            rec.child_count = counts.get(rec.id, 0)

    def _compute_journal_item_count(self):
        move_line_model = self.env["account.move.line"].sudo()
        for rec in self:
            rec.journal_item_count = move_line_model.search_count(
                [("account_id.qbo_standard_account_id", "=", rec.id)],
            )

    @api.depends("category")
    def _compute_color(self):
        for rec in self:
            rec.color = CATEGORY_COLOR.get(rec.category, 0)

    @api.depends("category")
    def _compute_category_slug(self):
        for rec in self:
            rec.category_slug = (rec.category or "").strip().lower().replace(" ", "-")

    @api.constrains("parent_id")
    def _check_recursion_parent(self):
        if not self._check_recursion():
            raise ValidationError(_("A master chart account cannot be its own ancestor."))

    @api.constrains("entry_type", "parent_id")
    def _check_parent_consistency(self):
        for rec in self:
            if rec.entry_type == "header" and rec.parent_id:
                raise ValidationError(
                    _('Header account "%s" cannot have a parent.') % rec.name,
                )
            if rec.entry_type == "detail" and rec.parent_id and rec.parent_id.entry_type != "header":
                raise ValidationError(
                    _('Detail account "%s" must use a header account as parent.') % rec.name,
                )

    @api.model
    def import_chart_rows(self, rows):
        stats = {"created": 0, "updated": 0, "parents_linked": 0}
        existing = {
            (rec.code, rec.entry_type): rec
            for rec in self.with_context(active_test=False).search([])
        }
        for row in rows:
            vals = self._prepare_vals_from_csv_row(row)
            key = (vals["code"], vals["entry_type"])
            record = existing.get(key)
            if record:
                record.write(vals)
                stats["updated"] += 1
            else:
                record = self.create(vals)
                existing[key] = record
                stats["created"] += 1

        stats["parents_linked"] = self._resolve_parent_links()
        return stats

    @api.model
    def import_chart_from_bytes(self, csv_bytes):
        text = csv_bytes.decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(text)))
        if not rows:
            raise UserError(_("The uploaded CSV did not contain any rows."))
        return self.import_chart_rows(rows)

    @api.model
    def import_bundled_chart(self):
        # The master chart is driven exclusively by the current frozen kernel
        # JSON. A missing kernel is blocking for the US GAAP route: fail closed
        # with a clear, actionable error rather than silently seeding a stale
        # CSV that would put the chart out of sync with the normative kernel.
        kernel_path = self._require_kernel_json_path()
        return self.import_poseidon_kernel_json(kernel_path=kernel_path)

    @api.model
    def import_poseidon_kernel_json(self, kernel_path=None, layer=None):
        kernel_path = Path(kernel_path) if kernel_path else self._require_kernel_json_path()
        if not kernel_path.exists():
            raise UserError(_("Kernel file %s does not exist.") % kernel_path)

        data = json.loads(kernel_path.read_text(encoding="utf-8"))
        metadata = data.get("metadata") or {}
        file_version = (metadata.get("kernel_version") or "").strip()
        if file_version != CURRENT_KERNEL_VERSION:
            raise UserError(
                _(
                    "Kernel file %(path)s declares version %(found)s, but the US "
                    "GAAP route mandates the frozen kernel %(expected)s. Refusing "
                    "to import a non-current kernel.",
                )
                % {
                    "path": kernel_path,
                    "found": file_version or _("(none)"),
                    "expected": CURRENT_KERNEL_VERSION,
                },
            )

        default_layer = layer or self.env["ir.config_parameter"].sudo().get_param(
            "poseidon.kernel.default_layer",
            "L1",
        )
        # required=true lives on L0 only. Derive the required codes straight from
        # the JSON L0 so they are marked even when the operational layer is L1/L2
        # (where the flag is absent) — including the very first bootstrap when no
        # L0 records exist in the DB yet.
        l0_required_codes = self._kernel_l0_required_codes(data)
        rows = self._kernel_accounts_for_layer(data, default_layer)
        stats = self.import_kernel_rows(
            rows, metadata, default_layer, l0_required_codes=l0_required_codes,
        )
        self._ensure_poseidon_kernel_versions(data)
        return stats

    @api.model
    def bootstrap_default_poseidon_kernel(self):
        stats = self.import_bundled_chart()
        l3_stats = self._ensure_l3_master_chart_imported()
        publish_stats = self.publish_kernel_to_companies_missing_master_chart()
        metadata_stats = self.apply_kernel_metadata_to_linked_company_accounts()
        return {
            "standard": stats,
            "l3": l3_stats,
            "publish": publish_stats,
            "metadata": metadata_stats,
        }

    @api.model
    def _default_kernel_json_path(self):
        # Explicit override wins: deployments where docs/ is not co-located
        # with the addon point here via the system parameter.
        override = self.env["ir.config_parameter"].sudo().get_param(KERNEL_PATH_CONFIG_PARAM)
        if override:
            path = Path(override)
            return path if path.exists() else None

        module_path = get_module_path("qbo_bridge_standard_chart")
        if not module_path:
            return None
        module_path = Path(module_path)
        # Discovery for the CURRENT kernel only — never a generic "kernel.json".
        # An older/renamed artifact must not satisfy the lookup.
        candidates = [module_path / "data" / CURRENT_KERNEL_FILENAME]
        for base in (module_path, module_path.resolve()):
            for parent in base.parents:
                candidates.extend(
                    parent.joinpath(*doc_dir) / CURRENT_KERNEL_FILENAME
                    for doc_dir in KERNEL_DOC_DIRS
                )
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    @api.model
    def _require_kernel_json_path(self):
        path = self._default_kernel_json_path()
        if not path:
            raise UserError(
                _(
                    "The current Poseidon kernel (%(filename)s) could not be "
                    "located. The US GAAP route requires kernel %(version)s. "
                    "Place it under docs/reference/accounting/ alongside the "
                    "backend, or set the system parameter '%(param)s' to its "
                    "absolute path.",
                )
                % {
                    "filename": CURRENT_KERNEL_FILENAME,
                    "version": CURRENT_KERNEL_VERSION,
                    "param": KERNEL_PATH_CONFIG_PARAM,
                },
            )
        return path

    @api.model
    def _default_l3_kernel_json_path(self):
        # Explicit override wins: deployments where docs/ is not co-located
        # with the addon point here via the system parameter.
        override = self.env["ir.config_parameter"].sudo().get_param(L3_KERNEL_PATH_CONFIG_PARAM)
        if override:
            path = Path(override)
            return path if path.exists() else None

        module_path = get_module_path("qbo_bridge_standard_chart")
        if not module_path:
            return None
        module_path = Path(module_path)
        # Discovery for the L3 derived kernel only — never a generic "kernel.json".
        # Prefer module data/, fall back to the documentation locations (where
        # the artifact ships), mirroring the L0/L1 kernel discovery.
        candidates = [module_path / "data" / L3_KERNEL_FILENAME]
        for base in (module_path, module_path.resolve()):
            for parent in base.parents:
                candidates.extend(
                    parent.joinpath(*doc_dir) / L3_KERNEL_FILENAME
                    for doc_dir in KERNEL_DOC_DIRS
                )
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    @api.model
    def _assert_l3_artifact_governed(self, kernel_path, metadata):
        """Refuse an L3 artifact that is not THE derived expansion of this kernel.

        L3 cannot be checked the way L0/L1/L2 are: it is derived, so it declares
        its own version (2025.3-L3.1) and can never equal CURRENT_KERNEL_VERSION.
        Its governance is the derivation link instead — the artifact must say
        which frozen kernel it expands, and that must be the one this backend is
        bound to. An L3 built from another kernel carries codes that no longer
        roll up into the L0/L1 accounts in this database.

        Status is deliberately NOT gated here: whatever the artifact declares is
        RECORDED as-is (see _ensure_poseidon_kernel_versions), so a database
        always reports what it actually carries. L3 never becomes the installed
        kernel either way — poseidon.kernel.version excludes the layer.
        """
        declared = (metadata.get("kernel_version") or "").strip()
        derived_from = (metadata.get("derived_from_kernel_version") or "").strip()
        if declared != L3_KERNEL_VERSION:
            raise UserError(
                _(
                    "L3 kernel file %(path)s declares version %(found)s, but this "
                    "backend expects the derived kernel %(expected)s. Refusing to "
                    "seed an unrecognised L3 expansion.",
                )
                % {
                    "path": kernel_path,
                    "found": declared or _("(none)"),
                    "expected": L3_KERNEL_VERSION,
                },
            )
        if derived_from != CURRENT_KERNEL_VERSION:
            raise UserError(
                _(
                    "L3 kernel file %(path)s is derived from kernel %(found)s, but "
                    "this route is bound to the frozen kernel %(expected)s. An L3 "
                    "expansion of another kernel would not roll up into this "
                    "chart.",
                )
                % {
                    "path": kernel_path,
                    "found": derived_from or _("(none)"),
                    "expected": CURRENT_KERNEL_VERSION,
                },
            )

    @api.model
    def import_poseidon_l3_kernel(self, path=None):
        """Seed the L3 analytic derived kernel into the master chart.

        Lenient and idempotent by design: a missing artifact leaves the master
        chart untouched (L3 simply becomes unavailable) instead of blocking the
        flow. Preserved L0/L1 accounts are NEVER re-tagged — their kernel layer,
        version and required flag stay exactly as imported from the frozen L0/L1
        kernel; the L3 artifact only expands them with activity/functional
        metadata. The single validation is that the preserved set covers the
        L0-required accounts (the artifact must reproduce the full kernel).
        """
        stats = {
            "available": True,
            "created": 0,
            "updated": 0,
            "preserved": 0,
            "parents_linked": 0,
        }
        kernel_path = Path(path) if path else self._default_l3_kernel_json_path()
        if not kernel_path or not kernel_path.exists():
            stats["available"] = False
            return stats

        data = json.loads(kernel_path.read_text(encoding="utf-8"))
        metadata = data.get("metadata") or {}
        # Leniency covers a MISSING artifact (handled above), never a wrong one.
        # The path is operator-settable (poseidon.kernel.l3_json_path), so
        # without this the master chart would absorb whatever JSON happens to
        # sit there — including an L3 derived from a different kernel, which
        # would silently break the rollup into L0/L1.
        self._assert_l3_artifact_governed(kernel_path, metadata)
        rows = list(((data.get("kernels") or {}).get("L3") or {}).get("accounts") or [])
        if not rows:
            return stats

        dash_sets = self._l3_dashboard_sets_reverse(data)
        preserved_codes = {
            (row.get("code") or "").strip()
            for row in rows
            if (row.get("layer") or "").strip().upper() in {"L0", "L1", "L0/L1"}
        }
        required_codes = {
            rec.code
            for rec in self.with_context(active_test=False).search(
                [("kernel_layer", "=", "L0"), ("kernel_required", "=", True)],
            )
        }
        # The ONLY validation: the preserved set must cover the L0-required
        # accounts. L3 never fail-closes on required-L0 for the expansion itself
        # (missing L3 accounts are an absence of optional depth, not a defect).
        if required_codes and not required_codes <= preserved_codes:
            missing = sorted(required_codes - preserved_codes)
            raise UserError(
                _(
                    "The L3 kernel file does not preserve the required L0 "
                    "accounts: %(missing)s. Refusing to seed an incomplete L3 "
                    "expansion.",
                )
                % {"missing": ", ".join(missing)},
            )

        existing = {
            (rec.code, rec.entry_type): rec
            for rec in self.with_context(active_test=False).search([])
        }
        imported_ids = self.browse()
        for row in rows:
            is_new_l3 = (row.get("layer") or "").strip().upper() == "L3"
            code = (row.get("code") or "").strip()
            enriched = dict(row)
            enriched["dashboard_account_sets"] = ",".join(sorted(dash_sets.get(code, ())))
            vals = self._prepare_vals_from_kernel_row(
                enriched,
                metadata,
                "L3",
                set(),
                analytic=is_new_l3,
            )
            key = (vals["code"], vals["entry_type"])
            # An L3 header that a DRAFT-era import wrote as a detail row still
            # has to be found, or this would create a second row for the same
            # code instead of flipping entry_type on the existing one.
            record = (
                existing.get(key)
                or existing.get((vals["code"], "detail"))
                or existing.get((vals["code"], "header"))
            )
            if record:
                if not is_new_l3:
                    # Preserved L0/L1 rows: keep their kernel identity intact.
                    vals.pop("kernel_layer", None)
                    vals.pop("kernel_version", None)
                    vals.pop("kernel_required", None)
                    stats["preserved"] += 1
                record.write(vals)
                stats["updated"] += 1
            else:
                if not is_new_l3:
                    # Fresh-DB fallback (should not happen after the L0/L1
                    # bootstrap): a preserved account missing from the DB enters
                    # as a plain L1 non-required account.
                    vals["kernel_layer"] = "L1"
                    vals["kernel_version"] = CURRENT_KERNEL_VERSION
                    vals["kernel_required"] = False
                record = self.create(vals)
                existing[key] = record
                stats["created"] += 1
            imported_ids |= record

        stats["parents_linked"] = self._resolve_parent_links()
        self._ensure_poseidon_kernel_versions(data)
        self.env["ir.config_parameter"].sudo().set_param(
            L3_KERNEL_ENRICHMENT_PARAM,
            L3_KERNEL_ENRICHMENT_VERSION,
        )
        return stats

    @api.model
    def _l3_dashboard_sets_reverse(self, data):
        reverse = {}
        expansion = data.get("dashboard_account_sets_l3_expansion") or {}
        for set_key, spec in expansion.items():
            for child in ((spec or {}).get("l3_analytic_children") or []):
                reverse.setdefault(child, set()).add(set_key)
        return reverse

    @api.model
    def _ensure_l3_master_chart_imported(self):
        """Self-heal (lazy + lenient): seed the L3 analytic kernel if missing.

        Refreshes metadata once when the artifact gains YAML enrichment, then
        stays idempotent. A missing artifact is not an error — L3 stays
        unavailable and the flow continues (unlike the frozen L0/L1 kernel,
        which fails closed).
        """
        has_rows = self.with_context(active_test=False).search_count(
            [("kernel_layer", "=", "L3")],
        )
        if has_rows:
            if (
                self.env["ir.config_parameter"].sudo().get_param(
                    L3_KERNEL_ENRICHMENT_PARAM,
                )
                == L3_KERNEL_ENRICHMENT_VERSION
            ):
                return {"available": True, "already_seeded": True}
            stats = self.import_poseidon_l3_kernel()
            stats["already_seeded"] = False
            return stats
        stats = self.import_poseidon_l3_kernel()
        stats["already_seeded"] = False
        return stats

    @api.model
    def _kernel_l0_required_codes(self, data):
        l0 = (data.get("kernels") or {}).get("L0") or {}
        return {
            (account.get("code") or "").strip()
            for account in (l0.get("accounts") or [])
            if account.get("required")
        }

    @api.model
    def _kernel_accounts_for_layer(self, data, layer):
        kernels = data.get("kernels") or {}
        kernel = kernels.get(layer)
        if not kernel:
            raise UserError(
                _("Kernel layer %(layer)s was not found in %(filename)s.")
                % {"layer": layer, "filename": CURRENT_KERNEL_FILENAME},
            )
        return list(kernel.get("accounts") or [])

    @api.model
    def import_kernel_rows(self, rows, metadata, layer, l0_required_codes=None):
        stats = {"created": 0, "updated": 0, "parents_linked": 0, "archived": 0}
        existing = {
            (rec.code, rec.entry_type): rec
            for rec in self.with_context(active_test=False).search([])
        }
        imported_ids = self.browse()
        # required=true is declared on L0 only. Start from any in-layer flag, then
        # union the L0 required set derived from the kernel JSON so required L0
        # accounts are marked even when the operational layer is L1/L2.
        required_codes = {
            (row.get("code") or "").strip()
            for row in rows
            if row.get("required")
        }
        if l0_required_codes:
            required_codes |= {code for code in l0_required_codes if code}
        elif not required_codes and layer != "L0":
            # Direct callers that don't pass the L0 set fall back to the DB —
            # only useful after L0 has been imported at least once.
            required_codes = {
                rec.code
                for rec in self.with_context(active_test=False).search(
                    [("kernel_layer", "=", "L0"), ("kernel_required", "=", True)],
                )
            }

        # Fail-closed materialization: every required L0 account must be present
        # in the operational layer being imported, or the published chart would
        # silently drop a mandatory account. The frozen kernel guarantees
        # K_L0 ⊆ K_L1 and K_L2 carries the same codes, so this only trips on a
        # malformed/incomplete kernel — exactly when import must be blocked.
        if l0_required_codes:
            present_codes = {(row.get("code") or "").strip() for row in rows}
            missing = {code for code in l0_required_codes if code} - present_codes
            if missing:
                raise UserError(
                    _(
                        "Kernel layer %(layer)s is missing required L0 accounts: "
                        "%(missing)s. Refusing to publish an incomplete chart.",
                    )
                    % {"layer": layer, "missing": ", ".join(sorted(missing))},
                )

        for row in rows:
            vals = self._prepare_vals_from_kernel_row(row, metadata, layer, required_codes)
            key = (vals["code"], vals["entry_type"])
            record = existing.get(key)
            if record:
                record.write(vals)
                stats["updated"] += 1
            else:
                record = self.create(vals)
                existing[key] = record
                stats["created"] += 1
            imported_ids |= record

        stats["parents_linked"] = self._resolve_parent_links()
        stats["archived"] = self._archive_unlinked_non_kernel_rows(imported_ids)
        return stats

    @api.model
    def _archive_unlinked_non_kernel_rows(self, imported_records):
        imported_ids = set(imported_records.ids)
        candidates = self.with_context(active_test=False).search(
            [
                ("id", "not in", list(imported_ids) or [0]),
                ("active", "=", True),
                ("kernel_layer", "=", False),
            ],
        )
        archived = 0
        Account = self.env["account.account"].sudo().with_context(active_test=False)
        Rule = self.env["qbo.account.bridge.rule"].sudo().with_context(active_test=False)
        for record in candidates:
            linked = Account.search_count([("qbo_standard_account_id", "=", record.id)], limit=1)
            rules = Rule.search_count([("standard_account_id", "=", record.id)], limit=1)
            if linked or rules:
                continue
            record.active = False
            archived += 1
        return archived

    @api.model
    def _prepare_vals_from_kernel_row(self, row, metadata, layer, required_codes, analytic=None):
        category_key = (row.get("category") or "").strip().upper()
        category = KERNEL_CATEGORY_LABELS.get(category_key, category_key.title())
        code = (row.get("code") or "").strip()
        name = (row.get("name") or "").strip()
        normal_balance = (row.get("normal_balance") or "").strip().lower()
        if not normal_balance:
            normal_balance = self._kernel_normal_balance(
                category_key, name, row.get("subcategory"),
            )
        included_reason = row.get("included_reason") or ""
        notes = []
        if row.get("notes"):
            notes.append(row["notes"])
        if included_reason:
            notes.append(included_reason)
        if row.get("collapse_rule"):
            notes.append(_("Collapse rule: %s") % row["collapse_rule"])
        if row.get("derived_from"):
            notes.append(_("Derived from: %s") % ", ".join(row["derived_from"]))
        if row.get("activity_tag"):
            notes.append(_("L3 activity tag: %s") % row["activity_tag"])
        if row.get("functional_group"):
            notes.append(_("L3 functional group: %s") % row["functional_group"])
        if row.get("rollup_to_l1_parent"):
            notes.append(_("Rolls up into L0/L1 account %s") % row["rollup_to_l1_parent"])
        if row.get("status"):
            notes.append(_("L3 kernel status: %s") % row["status"])
        if row.get("seed_ready"):
            notes.append(_("L3 seed-ready."))
        if row.get("posting_allowed") is False:
            notes.append(_("L3 posting not allowed (kernel header/summary)."))
        notes.append(
            _("Poseidon kernel %(version)s %(layer)s.") % {
                "version": metadata.get("kernel_version") or "",
                "layer": layer,
            },
        )

        activity_tag = (row.get("activity_tag") or "").strip() or False
        if activity_tag:
            parts = []
            for token in activity_tag.split("/"):
                canonical = L3_ACTIVITY_TAG_TOKEN_MAP.get(token.strip(), token.strip())
                if canonical and canonical not in parts:
                    parts.append(canonical)
            activity_tag = " / ".join(parts) or False
        functional_group = (row.get("functional_group") or "").strip() or False
        rollup_to_l1_parent = (row.get("rollup_to_l1_parent") or "").strip() or False
        dashboard_account_sets = (row.get("dashboard_account_sets") or "").strip() or False
        is_l3 = (row.get("layer") or "").strip().upper() == "L3" or layer == "L3"
        # `analytic` distinguishes a NEW L3 account from an L0/L1 account merely
        # carried inside the L3 artifact. Only the former may be demoted to a
        # header: 63000-67000 declare posting_allowed=false at L3 depth, but
        # they belong to the frozen kernel and companies already post to them.
        is_new_l3 = is_l3 if analytic is None else bool(analytic)

        tag_parts = [
            "poseidon",
            "kernel",
            layer.lower(),
            (metadata.get("gaap_basis") or "").lower().replace("_", "-"),
            category.lower().replace(" ", "-"),
        ]
        if is_l3:
            tag_parts.extend(["l3", "analytic"])
            if activity_tag:
                tag_parts.append(activity_tag.lower().replace("/", "-").replace(" ", "-"))

        long_description = row.get("description") or included_reason or False
        detailed_description = row.get("description") or included_reason or False
        if is_l3:
            long_description = (
                row.get("description_simple") or long_description
            )
            detailed_description = (
                row.get("description_detailed") or detailed_description
            )

        # A kernel account that carries L3 children declares posting_allowed
        # false. entry_type=header is what actually enforces it: activation,
        # publication and the sync wizard all filter on entry_type=detail, so a
        # header can never become a company account or receive a posting.
        entry_type = "header" if (is_new_l3 and row.get("posting_allowed") is False) else "detail"

        return {
            "active": True,
            "code": code,
            "description": name,
            "long_description": long_description,
            "entry_type": entry_type,
            "category": category,
            "fs_mapping": row.get("fs_mapping") or KERNEL_FS_MAPPING.get(category_key) or False,
            "parent_code": (row.get("parent_code") or "").strip() or False,
            "normal_balance": normal_balance,
            "tags": ", ".join(part for part in tag_parts if part),
            "notes": "\n".join(notes),
            "gaap_classification": metadata.get("gaap_basis") or False,
            "detailed_description": detailed_description,
            # Declared wins. The name heuristic below is a fallback for the
            # frozen L0/L1/L2 kernel, which does not carry the field; it typed
            # every PP&E account as a current asset, the AR aging buckets as
            # non-receivable and the trade AP detail as non-payable, so the L3
            # artifact states it outright instead.
            "odoo_account_type": (
                (row.get("odoo_account_type") or "").strip()
                or self._guess_odoo_account_type_from_kernel_row(row)
            ),
            "kernel_version": metadata.get("kernel_version") or False,
            "kernel_layer": layer,
            "kernel_required": bool(row.get("required") or code in required_codes),
            "master_account_id": row.get("master_account_id") or False,
            "activity_tag": activity_tag,
            "functional_group": functional_group,
            "rollup_to_l1_parent": rollup_to_l1_parent,
            "analytic": is_new_l3,
            "dashboard_account_sets": dashboard_account_sets,
        }

    @api.model
    def _kernel_normal_balance(self, category_key, name, subcategory=None):
        # A contra account's normal balance is the opposite of its category's.
        # The frozen kernel spells normal_balance out for 15900 and 49000 but
        # not for 13000 (Allowance for Doubtful Accounts), which carries only
        # subcategory=CONTRA_ASSET — so without this it imported as a debit.
        if (subcategory or "").strip().upper().startswith("CONTRA"):
            return "credit" if KERNEL_NORMAL_BALANCE.get(category_key) == "debit" else "debit"
        if "accumulated depreciation" in (name or "").casefold():
            return "credit"
        return KERNEL_NORMAL_BALANCE.get(category_key, "debit")

    @api.model
    def _guess_odoo_account_type_from_kernel_row(self, row):
        category = (row.get("category") or "").strip().upper()
        name = (row.get("name") or "").strip().casefold()
        code = (row.get("code") or "").strip()

        if category == "ASSET":
            if "receivable" in name:
                return "asset_receivable"
            if any(token in name for token in ["cash", "bank", "undeposited"]):
                return "asset_cash"
            if "prepaid" in name:
                return "asset_prepayments"
            if any(token in name for token in ["fixed", "depreciation", "intangible"]):
                return "asset_fixed"
            return "asset_current"

        if category == "LIABILITY":
            if "payable" in name and "tax" not in name:
                return "liability_payable"
            if "credit card" in name:
                return "liability_credit_card"
            if "long-term" in name or code.startswith("25"):
                return "liability_non_current"
            return "liability_current"

        if category == "EQUITY":
            if "current period earnings" in name:
                return "equity_unaffected"
            return "equity"

        if category == "REVENUE":
            return "income"

        if category == "COST_OF_GOODS_SOLD":
            return "expense_direct_cost"

        if category == "EXPENSE":
            if any(token in name for token in ["depreciation", "amortization"]):
                return "expense_depreciation"
            return "expense"

        return "off_balance"

    @api.model
    def _kernel_status_from_metadata(self, metadata):
        """Map an artifact's declared status onto the version record.

        Anything that is not explicitly a draft stays frozen: the frozen kernel
        declares no status at all, and an unknown value must not silently
        downgrade a normative kernel to something mutable.
        """
        declared = (metadata.get("status") or "").strip().upper()
        return "draft" if declared == "DRAFT" else "frozen"

    @api.model
    def _ensure_poseidon_kernel_versions(self, data):
        if "poseidon.kernel.version" not in self.env:
            return
        metadata = data.get("metadata") or {}
        version_model = self.env["poseidon.kernel.version"].sudo()
        kernel_version = metadata.get("kernel_version") or CURRENT_KERNEL_VERSION
        effective_from = metadata.get("effective_from") or fields.Date.today()
        for layer, kernel in (data.get("kernels") or {}).items():
            existing = version_model.search(
                [
                    ("kernel_version", "=", kernel_version),
                    ("kernel_layer", "=", layer),
                ],
                limit=1,
            )
            checksum = hashlib.sha256(
                json.dumps(
                    {
                        "metadata": metadata,
                        "layer": layer,
                        "kernel": kernel,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode(),
            ).hexdigest()
            status = self._kernel_status_from_metadata(metadata)
            if existing:
                # A frozen record is immutable and authoritative — never touched.
                # A draft, though, must track its own artifact: when the artifact
                # is revised in place (2025.3-L3.1 DRAFT -> FROZEN) the record has
                # to follow, or the database keeps reporting a status and checksum
                # that no longer describe what it carries. Refreshing beats
                # delete-and-recreate: company accounts FK-reference this row
                # through account_account.poseidon_kernel_version_id, so deleting
                # it is impossible on any database that has published accounts.
                if existing.status != "frozen" and (
                    existing.status != status or existing.checksum != checksum
                ):
                    existing.write(
                        {
                            "status": status,
                            "checksum": checksum,
                            "effective_from": effective_from,
                            "notes": metadata.get("notes") or False,
                        },
                    )
                continue
            version_model.create(
                {
                    "name": _("Kernel %(version)s %(layer)s") % {
                        "version": kernel_version,
                        "layer": layer,
                    },
                    "kernel_version": kernel_version,
                    "gaap_basis": metadata.get("gaap_basis") or "US_GAAP",
                    "kernel_layer": layer,
                    # The artifact's own declared status. Stamping "frozen"
                    # unconditionally recorded the DRAFT L3 kernel as frozen —
                    # which both misreports what the database carries and makes
                    # the record immutable, so the draft could never be
                    # superseded by its own successor.
                    "status": status,
                    "effective_from": effective_from,
                    "checksum": checksum,
                    "active": True,
                    "notes": metadata.get("notes") or False,
                },
            )

    @api.model
    def _prepare_vals_from_csv_row(self, row):
        entry_type = (row.get("type") or "detail").strip().lower()
        normal_balance = (row.get("normal_balance") or "debit").strip().lower()
        return {
            "active": not bool((row.get("end_date") or "").strip()),
            "code": (row.get("code") or "").strip(),
            "description": (row.get("description") or "").strip(),
            "long_description": row.get("long_description") or False,
            "entry_type": entry_type,
            "category": (row.get("category") or "").strip(),
            "fs_mapping": (row.get("fs_mapping") or "").strip(),
            "parent_code": (row.get("parent_code") or "").strip() or False,
            "normal_balance": normal_balance,
            "tags": row.get("tags") or False,
            "default_vendors": row.get("default_vendors") or False,
            "regulatory_mapping": row.get("regulatory_mapping") or False,
            "start_date": (row.get("start_date") or "").strip() or False,
            "end_date": (row.get("end_date") or "").strip() or False,
            "notes": row.get("notes") or False,
            "subcategory": (row.get("subcategory") or "").strip() or False,
            "cash_flow_classification": (
                (row.get("cash_flow_classification") or "").strip() or False
            ),
            "cost_center": (row.get("cost_center") or "").strip() or False,
            "gaap_classification": (row.get("GAAP_classification") or "").strip() or False,
            "detailed_description": row.get("detailed_description") or False,
            "odoo_account_type": self._guess_odoo_account_type(row),
        }

    @api.model
    def _resolve_parent_links(self):
        headers = {
            rec.code: rec
            for rec in self.search([("entry_type", "=", "header")])
        }
        updates = 0
        for rec in self.search([("parent_code", "!=", False)]):
            parent = headers.get(rec.parent_code)
            parent_id = parent.id if parent else False
            if rec.parent_id.id != parent_id:
                rec.parent_id = parent_id
                updates += 1
        return updates

    @api.model
    def _guess_odoo_account_type(self, row):
        category = (row.get("category") or "").strip().lower()
        subcategory = (row.get("subcategory") or "").strip().lower()
        description = (row.get("description") or "").strip().lower()
        normal_balance = (row.get("normal_balance") or "").strip().lower()
        signature = " ".join(filter(None, [subcategory, description]))

        if category == "asset":
            if "receivable" in signature:
                return "asset_receivable"
            if any(token in signature for token in ["cash", "bank", "deposit"]):
                return "asset_cash"
            if "prepaid" in signature:
                return "asset_prepayments"
            if any(token in signature for token in ["fixed", "property", "equipment"]):
                return "asset_fixed"
            if any(token in signature for token in ["long-term", "non-current", "investment"]):
                return "asset_non_current"
            return "asset_current"

        if category == "liability":
            if "payable" in signature:
                return "liability_payable"
            if "credit card" in signature:
                return "liability_credit_card"
            if any(token in signature for token in ["long-term", "non-current", "loan", "debt"]):
                return "liability_non_current"
            return "liability_current"

        if category == "equity":
            return "equity"

        if category == "revenue":
            return "income"

        if category == "cost of goods sold":
            return "expense_direct_cost"

        if category == "expense":
            if any(token in signature for token in ["depreciation", "amortization"]):
                return "expense_depreciation"
            return "expense"

        if category == "other":
            return "income_other" if normal_balance == "credit" else "expense_other"

        return "off_balance"

    def action_view_bridge_rules(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Bridge Rules - %s") % self.display_name,
            "res_model": "qbo.account.bridge.rule",
            "view_mode": "list,form",
            "domain": [("standard_account_id", "=", self.id)],
        }

    def action_view_linked_accounts(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Company Accounts - %s") % self.display_name,
            "res_model": "account.account",
            "view_mode": "list,form",
            "domain": [("qbo_standard_account_id", "=", self.id)],
        }

    def action_view_child_accounts(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Child Accounts - %s") % self.display_name,
            "res_model": "qbo.standard.account",
            "view_mode": "list,kanban,form",
            "domain": [("parent_id", "=", self.id)],
            "context": {"default_parent_id": self.id},
        }

    def action_view_journal_items(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Journal Items - %s") % self.display_name,
            "res_model": "account.move.line",
            "view_mode": "list,form",
            "domain": [("account_id.qbo_standard_account_id", "=", self.id)],
        }

    def action_open_sync_wizard(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Sync Master Account"),
            "res_model": "qbo.standard.account.sync.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_standard_account_id": self.id},
        }

    def action_open_import_wizard(self):
        return {
            "type": "ir.actions.act_window",
            "name": _("Import Master Chart"),
            "res_model": "qbo.standard.chart.import.wizard",
            "view_mode": "form",
            "target": "new",
        }

    def prepare_company_account_vals(self, company):
        self.ensure_one()
        note = self.long_description or self.detailed_description or self.notes
        vals = {
            "name": self.description,
            "code": self.code,
            "account_type": self.odoo_account_type,
            "description": self.detailed_description or self.long_description or False,
            "note": note,
            "active": self.active,
            "company_ids": [(4, company.id)],
            "qbo_standard_account_id": self.id,
        }
        vals.update(self._poseidon_company_account_metadata_vals())
        return vals

    def _poseidon_company_account_metadata_vals(self):
        self.ensure_one()
        account_fields = self.env["account.account"]._fields
        vals = {}
        if "poseidon_kernel_version_id" in account_fields and self.kernel_version:
            version = self.env["poseidon.kernel.version"].sudo().search(
                [
                    ("kernel_version", "=", self.kernel_version),
                    ("kernel_layer", "=", self.kernel_layer or "L1"),
                ],
                limit=1,
            )
            if version:
                vals["poseidon_kernel_version_id"] = version.id
        optional_fields = {
            "poseidon_kernel_layer": self.kernel_layer,
            "poseidon_kernel_code": self.code if self.kernel_layer else False,
            "poseidon_kernel_required": self.kernel_required,
            "poseidon_master_account_id": self.master_account_id,
            "poseidon_account_category": self._poseidon_account_category_value(),
            "poseidon_normal_balance": self._poseidon_normal_balance_value(),
            "poseidon_fs_mapping": self.fs_mapping,
        }
        for field_name, value in optional_fields.items():
            if field_name in account_fields and value not in (None, False, ""):
                vals[field_name] = value
        return vals

    def _poseidon_account_category_value(self):
        self.ensure_one()
        value = (self.category or "").strip().upper().replace(" ", "_")
        if value == "COST_OF_GOODS_SOLD":
            return value
        allowed = {"ASSET", "LIABILITY", "EQUITY", "REVENUE", "EXPENSE"}
        return value if value in allowed else False

    def _poseidon_normal_balance_value(self):
        self.ensure_one()
        if self.normal_balance == "debit":
            return "Debit"
        if self.normal_balance == "credit":
            return "Credit"
        return False

    @api.model
    def _ensure_master_chart_imported(self):
        """Self-heal: import the frozen kernel master chart if none is present.

        The publish primitives assume the master chart exists. On a fresh DB it
        does not (no install hook seeds it), which silently yields 0 accounts —
        the exact bug class this fix closes. Idempotent + version fail-closed.
        """
        has_rows = self.with_context(active_test=False).search_count(
            [("entry_type", "=", "detail"), ("kernel_layer", "!=", False)]
        )
        if not has_rows:
            self.import_bundled_chart()

    @api.model
    def sync_detail_accounts_to_company(self, company, update_existing=True, required_only=False):
        account_model = self.env["account.account"].with_company(company)
        stats = {"created": 0, "updated": 0, "skipped": 0, "blocked": 0}
        # L3 analytic accounts are never bulk-published: activation is on demand
        # via poseidon_activate_l3_accounts / poseidon_activate_l3_batch. The OR
        # keeps non-kernel (CSV-imported) detail accounts in scope.
        detail_domain = [
            ("entry_type", "=", "detail"),
            "|",
            ("kernel_layer", "!=", "L3"),
            ("kernel_layer", "=", False),
        ]
        if required_only:
            detail_domain.append(("kernel_required", "=", True))
        detail_accounts = self.search(detail_domain, order="code")
        for standard_account in detail_accounts:
            account = account_model.search(
                [
                    ("company_ids", "=", company.id),
                    ("qbo_standard_account_id", "=", standard_account.id),
                ],
                limit=1,
            )
            if not account:
                account = account_model.search(
                    [
                        ("company_ids", "=", company.id),
                        ("code", "=", standard_account.code),
                    ],
                    limit=1,
                )

            vals = standard_account.prepare_company_account_vals(company)
            if account:
                if not update_existing:
                    stats["skipped"] += 1
                    continue
                # Defense in depth: never refresh a Poseidon-locked company
                # account. The BFF preview classifies these as "blocked" and the
                # commit preflight refuses them, but a direct backend caller must
                # be protected here too. Skip + count rather than aborting the
                # whole publish (or tripping the coarse immutable-field write
                # guard). Field-presence guarded: this addon does not depend on
                # poseidon_accounting_kernel, so the lock field may be absent.
                if (
                    "poseidon_kernel_locked" in account._fields
                    and account.poseidon_kernel_locked
                ):
                    stats["blocked"] += 1
                    continue
                account.write(vals)
                stats["updated"] += 1
            else:
                account_model.create(vals)
                stats["created"] += 1

        return stats

    @api.model
    def publish_kernel_to_companies_missing_master_chart(self):
        stats = {
            "companies_seen": 0,
            "companies_published": 0,
            "companies_skipped": 0,
            "created": 0,
            "linked": 0,
            "metadata_updated": 0,
            "metadata_skipped": 0,
        }
        standards = self.search(
            [
                ("entry_type", "=", "detail"),
                ("kernel_layer", "!=", False),
                # L3 analytic accounts are excluded: they are never published in
                # bulk, only activated on demand (individual or batch).
                ("kernel_layer", "!=", "L3"),
                ("active", "=", True),
            ],
            order="code",
        )
        if not standards:
            return stats

        Account = self.env["account.account"].sudo().with_context(active_test=False)
        for company in self.env["res.company"].sudo().search([]):
            stats["companies_seen"] += 1
            linked_count = Account.search_count(
                [
                    ("company_ids", "in", [company.id]),
                    ("qbo_standard_account_id", "!=", False),
                ],
            )
            if linked_count:
                stats["companies_skipped"] += 1
                continue

            company_stats = self._publish_kernel_standards_to_company(company, standards)
            stats["companies_published"] += 1
            for key in ("created", "linked", "metadata_updated", "metadata_skipped"):
                stats[key] += company_stats[key]
        return stats

    @api.model
    def _publish_kernel_standards_to_company(self, company, standards):
        stats = {
            "created": 0,
            "linked": 0,
            "metadata_updated": 0,
            "metadata_skipped": 0,
        }
        Account = self.env["account.account"].sudo().with_company(company).with_context(active_test=False)
        for standard in standards:
            account = Account.search(
                [
                    ("company_ids", "in", [company.id]),
                    ("qbo_standard_account_id", "=", standard.id),
                ],
                limit=1,
            )
            if not account:
                account = Account.search(
                    [
                        ("company_ids", "in", [company.id]),
                        ("code", "=", standard.code),
                    ],
                    limit=1,
                )

            if account:
                if account.qbo_standard_account_id != standard:
                    account.write({"qbo_standard_account_id": standard.id})
                    stats["linked"] += 1
                if self._safe_apply_kernel_metadata(account, standard):
                    stats["metadata_updated"] += 1
                else:
                    stats["metadata_skipped"] += 1
            else:
                Account.create(standard.prepare_company_account_vals(company))
                stats["created"] += 1
        return stats

    @api.model
    def apply_kernel_metadata_to_linked_company_accounts(self):
        stats = {"updated": 0, "skipped": 0}
        Account = self.env["account.account"].sudo().with_context(active_test=False)
        accounts = Account.search(
            [
                ("qbo_standard_account_id.kernel_layer", "!=", False),
            ],
        )
        for account in accounts:
            if self._safe_apply_kernel_metadata(account, account.qbo_standard_account_id):
                stats["updated"] += 1
            else:
                stats["skipped"] += 1
        return stats

    @api.model
    def realign_l3_company_account_types(self):
        """Push the declared L3 Odoo account type onto already-published accounts.

        Re-importing the master chart fixes the master rows, but nothing rewrites
        a company account that was already published: sync_detail_accounts_to_company
        excludes L3 by design, activation only writes vals on create, and
        apply_kernel_metadata_to_linked_company_accounts carries the poseidon_*
        metadata, not account_type. So a tenant published under the DRAFT-era name
        heuristic keeps PP&E in current assets forever.

        A locked account is reported, never rewritten. account_type is one of
        POSEIDON_IMMUTABLE_FIELDS, and the kernel auto-locks an account on its
        first journal line, so "locked" already covers "carries posted entries" —
        and retyping under posted entries moves them between statement lines,
        which is an accounting decision, not a migration. The move-line check is
        the same guard for a deployment without poseidon_accounting_kernel, where
        no lock field exists.
        """
        stats = {"updated": 0, "unchanged": 0, "blocked": []}
        Account = self.env["account.account"].sudo().with_context(active_test=False)
        MoveLine = self.env["account.move.line"].sudo()
        for account in Account.search([("qbo_standard_account_id.kernel_layer", "=", "L3")]):
            standard = account.qbo_standard_account_id
            declared = standard.odoo_account_type
            if account.account_type == declared:
                stats["unchanged"] += 1
                continue
            locked = (
                "poseidon_kernel_locked" in account._fields
                and account.poseidon_kernel_locked
            ) or MoveLine.search_count([("account_id", "=", account.id)], limit=1)
            if locked:
                # standard.code, not account.code: account.account.code is
                # company-dependent in Odoo 19 and reads False without a company
                # in context. The kernel code is the same value and always set.
                stats["blocked"].append((standard.code, account.account_type, declared))
                continue
            account.write({"account_type": declared})
            stats["updated"] += 1
        return stats

    @api.model
    def _safe_apply_kernel_metadata(self, account, standard):
        vals = standard._poseidon_company_account_metadata_vals()
        if not vals:
            return True
        try:
            account.write(vals)
        except UserError:
            return False
        return True
