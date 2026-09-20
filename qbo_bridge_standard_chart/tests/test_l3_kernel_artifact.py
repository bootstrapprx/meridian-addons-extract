"""Validate the frozen L3 derived kernel artifact against its own guarantees.

Pure JSON checks — no database, no Odoo runtime — so this also runs standalone:

    python3 -m unittest backend/addons/poseidon/qbo_bridge_standard_chart/tests/test_l3_kernel_artifact.py

This is the gate that let 2025.3-L3.1 be frozen. Every claim in the artifact's
`guarantees` list is asserted here; if a future revision breaks one, this fails
before the importer ever sees the file.
"""

import json
import unittest
from pathlib import Path

try:
    # Odoo's test loader only collects odoo.tests.case.TestCase subclasses, so a
    # plain unittest.TestCase would be silently skipped. BaseCase needs no cursor
    # and these assertions touch no database.
    from odoo.tests.common import BaseCase as _TestBase
except ImportError:  # standalone run: python3 <this file>
    _TestBase = unittest.TestCase

def _find(*candidates):
    """Walk up to the repo root looking for any of the given relative paths.

    Parent-counting breaks because the addon is reached through a profile
    symlink of a different depth, so we search upwards instead.

    Returns None when nothing matches. It must NOT raise: this runs at module
    import time, and raising here aborts collection of the whole
    qbo_bridge_standard_chart suite before Odoo even selects test tags. That
    exact failure blocked the Odoo check on four consecutive AI Center parity
    phases (see docs/reference/meridian/ai-center-parity-matrix.md).
    """
    for parent in Path(__file__).resolve().parents:
        for relative in candidates:
            candidate = parent.joinpath(*relative)
            if candidate.exists():
                return candidate
    return None


# Documentation moved to docs/reference/accounting/ on 2026-08-14. The legacy
# locations are still accepted so the test works against an older checkout and
# against a container where only part of docs/ is mounted.
BASE_PATH = _find(
    ("docs", "reference", "accounting", "kernel_v2025.3.json"),
    ("docs", "accounting", "kernel_v2025.3.json"),
    ("docs", "odoo", "kernel_v2025.3.json"),
)
L3_PATH = _find(
    ("docs", "reference", "accounting", "kernel_v2025_3_L3_derived.json"),
    ("docs", "accounting", "kernel_v2025_3_L3_derived.json"),
)

_MISSING = [
    name
    for name, path in (("kernel_v2025.3.json", BASE_PATH),
                       ("kernel_v2025_3_L3_derived.json", L3_PATH))
    if path is None
]

DEFAULT_NORMAL_BALANCE = {
    "ASSET": "Debit",
    "LIABILITY": "Credit",
    "EQUITY": "Credit",
    "REVENUE": "Credit",
    "COST_OF_GOODS_SOLD": "Debit",
    "EXPENSE": "Debit",
}
# account_type -> the kernel category it must belong to.
ODOO_TYPE_CATEGORY = {
    "asset_receivable": "ASSET",
    "asset_cash": "ASSET",
    "asset_current": "ASSET",
    "asset_non_current": "ASSET",
    "asset_prepayments": "ASSET",
    "asset_fixed": "ASSET",
    "liability_payable": "LIABILITY",
    "liability_credit_card": "LIABILITY",
    "liability_current": "LIABILITY",
    "liability_non_current": "LIABILITY",
    "equity": "EQUITY",
    "equity_unaffected": "EQUITY",
    "income": "REVENUE",
    "income_other": "REVENUE",
    "expense": "EXPENSE",
    "expense_other": "EXPENSE",
    "expense_depreciation": "EXPENSE",
    "expense_direct_cost": "COST_OF_GOODS_SOLD",
}
VALID_ACTIVITY_TAGS = {"Universal", "Services", "Trade/Retail", "Manufacturing"}
# ACA reports gross A/R, so the allowance is excluded from its closure.
ACA_INTENTIONAL_EXCLUSIONS = {"13000"}


def _expected_normal_balance(category, subcategory):
    balance = DEFAULT_NORMAL_BALANCE[category]
    if (subcategory or "").upper().startswith("CONTRA"):
        return "Credit" if balance == "Debit" else "Debit"
    return balance


@unittest.skipIf(
    _MISSING,
    "kernel artifact(s) not reachable from this checkout: %s. "
    "Expected under docs/reference/accounting/. If this is a container run, "
    "docs/ is probably not mounted — skip, do not treat as a failure."
    % ", ".join(_MISSING),
)
class TestL3KernelArtifact(_TestBase):
    @classmethod
    def setUpClass(cls):
        cls.base = json.loads(BASE_PATH.read_text(encoding="utf-8"))
        cls.l3d = json.loads(L3_PATH.read_text(encoding="utf-8"))
        cls.accounts = cls.l3d["kernels"]["L3"]["accounts"]
        cls.by_code = {a["code"]: a for a in cls.accounts}
        cls.base_l0 = {a["code"]: a for a in cls.base["kernels"]["L0"]["accounts"]}
        cls.base_l1 = {a["code"]: a for a in cls.base["kernels"]["L1"]["accounts"]}
        cls.new_l3 = [a for a in cls.accounts if (a.get("layer") or "").upper() == "L3"]

    def _rho(self, code):
        """closure_rho: walk to the L0 ancestor; a self-parent terminates."""
        seen, cur = set(), code
        while True:
            self.assertNotIn(cur, seen, f"rollup cycle reached from {code}")
            seen.add(cur)
            if cur in self.base_l0:
                return cur
            parent = (self.by_code.get(cur) or {}).get("rollup_to_l1_parent")
            if not parent or parent == cur:
                return None  # terminal orphan: outside every dashboard set
            cur = parent

    # -- freeze gate ----------------------------------------------------------

    def test_the_artifact_is_frozen_and_bound_to_the_frozen_base_kernel(self):
        md = self.l3d["metadata"]
        self.assertEqual(md["status"], "FROZEN")
        self.assertEqual(md["kernel_version"], "2025.3-L3.1")
        self.assertEqual(self.base["metadata"]["status"], "FROZEN")
        self.assertEqual(
            md["derived_from_kernel_version"],
            self.base["metadata"]["kernel_version"],
            "an L3 derived from another kernel carries codes that do not roll up",
        )
        self.assertEqual(
            self.l3d["agent_specification"]["freeze_blockers"],
            [],
            "a frozen artifact cannot still declare freeze blockers",
        )

    def test_declared_counts_match_the_enumeration(self):
        counts = self.l3d["counts"]
        self.assertEqual(counts["L3_total"], len(self.accounts))
        self.assertEqual(counts["L3_new_analytic"], len(self.new_l3))
        self.assertEqual(
            counts["L3_preserved_from_kernel"], len(self.accounts) - len(self.new_l3),
        )
        self.assertEqual(len(self.by_code), len(self.accounts), "duplicate codes")
        uuids = {a["master_account_id"] for a in self.accounts}
        self.assertEqual(len(uuids), len(self.accounts), "duplicate master_account_id")

    # -- guarantee: the frozen L0/L1 kernel is reproduced unchanged ------------

    def test_every_l0_l1_account_is_preserved_field_by_field(self):
        for code, src in self.base_l1.items():
            got = self.by_code.get(code)
            self.assertIsNotNone(got, f"L1 account {code} is missing from the L3 chart")
            for field in ("name", "category", "master_account_id"):
                self.assertEqual(got[field], src[field], f"{code}.{field} drifted")
            expected = (
                src.get("normal_balance")
                or self.base_l0.get(code, {}).get("normal_balance")
                or _expected_normal_balance(src["category"], src.get("subcategory"))
            )
            self.assertEqual(got["normal_balance"], expected, f"{code}.normal_balance drifted")
        for code, src in self.base_l0.items():
            self.assertEqual(
                self.by_code[code]["normal_balance"], src["normal_balance"], f"{code} vs L0",
            )
        required = {c for c, a in self.base_l0.items() if a.get("required")}
        self.assertLessEqual(required, set(self.by_code), "required L0 accounts missing")

    # -- guarantee: rollups resolve ------------------------------------------

    def test_every_rollup_resolves_through_the_chart(self):
        for account in self.accounts:
            parent = account.get("rollup_to_l1_parent")
            self.assertTrue(parent, f"{account['code']} declares no rollup_to_l1_parent")
            self.assertIn(
                parent, self.by_code,
                f"{account['code']} rolls up to {parent}, which is not in this chart",
            )
            self._rho(account["code"])  # asserts the walk terminates without a cycle

    def test_non_operating_expense_does_not_close_into_tax_expense(self):
        # 70000 used to roll into 69000, so closure_rho reported interest
        # expense, impairments and FX losses as tax expense.
        self.assertEqual(self.by_code["70000"]["rollup_to_l1_parent"], "70000")
        for code in ("70010", "70020", "70050"):
            self.assertIsNone(self._rho(code), f"{code} must stay below the line")

    # -- guarantee: polarity --------------------------------------------------

    def test_contra_accounts_carry_the_opposite_polarity(self):
        for account in self.accounts:
            self.assertEqual(
                account["normal_balance"],
                _expected_normal_balance(account["category"], account.get("subcategory")),
                f"{account['code']} {account['name']!r} has the wrong polarity",
            )
        spec = self.l3d["agent_specification"]["polarity_rules"]["known_examples"]
        for group in spec.values():
            for code in group["accounts"]:
                self.assertTrue(
                    (self.by_code[code].get("subcategory") or "").upper().startswith("CONTRA"),
                    f"{code} is contra per the spec but declares no CONTRA_* subcategory",
                )

    # -- guarantee: Odoo posting types are declared, never guessed ------------

    def test_every_new_l3_account_declares_a_valid_odoo_account_type(self):
        for account in self.new_l3:
            account_type = account.get("odoo_account_type")
            self.assertIn(
                account_type, ODOO_TYPE_CATEGORY,
                f"{account['code']} {account['name']!r} declares no usable "
                f"odoo_account_type ({account_type!r})",
            )
            self.assertEqual(
                ODOO_TYPE_CATEGORY[account_type], account["category"],
                f"{account['code']} type {account_type} contradicts category "
                f"{account['category']}",
            )

    def test_the_types_the_name_heuristic_got_wrong_are_pinned(self):
        # Each of these was mistyped when odoo_account_type was inferred from the
        # account name. They are the reason the field is declared.
        for code, expected in (
            ("15010", "asset_fixed"),        # Land, was asset_current
            ("15090", "asset_fixed"),        # Construction in Progress
            ("12010", "asset_receivable"),   # AR aging bucket, was asset_current
            ("20010", "liability_payable"),  # trade AP detail, was liability_current
            ("26000", "liability_non_current"),  # long-term note, was liability_payable
            ("19020", "asset_non_current"),  # deferred tax asset, was asset_current
            ("48010", "income_other"),       # interest income, was income
            ("70010", "expense_other"),      # interest expense, was expense
        ):
            self.assertEqual(self.by_code[code]["odoo_account_type"], expected, code)

    # -- guarantee: headers cannot be posted to -------------------------------

    def test_accounts_with_children_are_declared_headers(self):
        parents = {
            a["rollup_to_l1_parent"]
            for a in self.accounts
            if a["rollup_to_l1_parent"] != a["code"]
        }
        for code in sorted(parents & {a["code"] for a in self.new_l3}):
            self.assertIs(
                self.by_code[code].get("posting_allowed"), False,
                f"{code} carries L3 children but does not declare posting_allowed=false",
            )

    # -- guarantee: dashboard closure parity ----------------------------------

    def test_declared_dashboard_children_equal_the_closure(self):
        expansion = self.l3d["dashboard_account_sets_l3_expansion"]
        for key, roots in self.l3d["dashboard_account_sets_l1"].items():
            roots = set(roots)
            closure = {
                a["code"]
                for a in self.accounts
                if self._rho(a["code"]) in roots and a["code"] not in roots
            }
            if key == "ACA":
                closure -= ACA_INTENTIONAL_EXCLUSIONS
            declared = set(expansion[key]["l3_analytic_children"])
            self.assertEqual(
                declared, closure,
                f"{key}: declaration and closure_rho disagree "
                f"(missing {sorted(closure - declared)}, extra {sorted(declared - closure)})",
            )

    # -- activity tags --------------------------------------------------------

    def test_activity_tags_are_canonical(self):
        for account in self.accounts:
            raw = account.get("activity_tag")
            self.assertTrue(raw, f"{account['code']} declares no activity_tag")
            self.assertEqual(raw, raw.strip(), f"{account['code']}: {raw!r} is not trimmed")
            self.assertNotIn(" / ", raw, f"{account['code']}: {raw!r} uses spaced separators")
            tokens, rest = [], raw.split("/")
            while rest:
                if rest[0] == "Trade" and len(rest) > 1 and rest[1] == "Retail":
                    tokens.append("Trade/Retail")
                    rest = rest[2:]
                    continue
                tokens.append(rest.pop(0))
            self.assertLessEqual(
                set(tokens), VALID_ACTIVITY_TAGS, f"{account['code']}: {raw!r}",
            )


if __name__ == "__main__":
    unittest.main()
