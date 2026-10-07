"""Company Provenance Guard for QuickBooks imports.

A valid OAuth authorization proves *the transaction* was authorised by *some*
QuickBooks company. It does not, on its own, prove that every object arriving
through that connection belongs to the Meridian company performing the import.
This module answers the second question at the import boundary:

    Does this data belong to the company performing this operation?

The answer is deliberately a three-valued result, never a boolean:

* ``MATCH``    — provenance resolves to the expected company; safe to persist.
* ``MISMATCH`` — provenance resolves to a different company; reject.
* ``UNKNOWN``  — provenance cannot be established; reject/quarantine.

``UNKNOWN`` is ALWAYS non-persistible. That is a contract of this module, not a
per-caller choice: callers decide *how* to reject (abort a sync, quarantine an
intake row), never *whether* they may persist.

The expected company is resolved from the authoritative execution context — the
mapping that owns the pull. A company id supplied by a client is never used to
establish identity here.
"""
from enum import Enum

from odoo import _

from .qbo_api_client import QBOApiClient


class ProvenanceResult(Enum):
    MATCH = "match"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"

    @property
    def persistible(self):
        """Only MATCH may ever be persisted. UNKNOWN is never persistible."""
        return self is ProvenanceResult.MATCH


def resolve_realm_companies(env, realm):
    """Return the Meridian companies a realm is authorised to serve."""
    return (
        env["qbo.company.mapping"]
        .sudo()
        .search([("realm_id", "=", realm.id)])
        .mapped("company_id")
    )


def verify_realm_binding(env, mapping):
    """Offline part of the guard: does the realm belong to the company?

    Resolves ``mapping.realm_id`` → company mappings and compares against the
    authoritative expected company (``mapping.company_id``). Also rejects a
    pristine ``AUTO_*`` placeholder realm, which has never been bound to a real
    Intuit company. No network access; safe to call in staging boundaries.
    """
    realm = mapping.realm_id.sudo()
    expected_company = mapping.company_id
    if not expected_company:
        return ProvenanceResult.UNKNOWN

    mapped_companies = resolve_realm_companies(env, realm)
    if not mapped_companies:
        return ProvenanceResult.UNKNOWN
    if expected_company not in mapped_companies:
        return ProvenanceResult.MISMATCH

    realm_code = (realm.realm_id or "").strip()
    if not realm_code or realm_code.startswith("AUTO_"):
        return ProvenanceResult.MISMATCH

    return ProvenanceResult.MATCH


def verify_company_provenance(env, mapping, live_company_info=None):
    """Verify that data pulled through ``mapping`` belongs to its company.

    ``mapping.company_id`` is the authoritative expected company: the mapping
    owns the execution. ``live_company_info`` is the CompanyInfo dict fetched
    live from QuickBooks; pass ``None`` when it could not be fetched, which
    yields ``UNKNOWN`` and therefore a rejection at every call site.
    """
    binding = verify_realm_binding(env, mapping)
    if binding is not ProvenanceResult.MATCH:
        return binding

    realm = mapping.realm_id.sudo()

    if live_company_info is None:
        return ProvenanceResult.UNKNOWN

    live_id = str(live_company_info.get("Id") or "").strip()
    if not live_id or live_id != realm.realm_id:
        return ProvenanceResult.MISMATCH

    # Name is a cross-check only, compared against the value attested at bind
    # time (never against the Odoo company name, which may legitimately differ
    # and may change after authorisation).
    attested = (mapping.qbo_authorized_company_name or "").strip()
    live_name = str(
        live_company_info.get("CompanyName")
        or live_company_info.get("LegalName")
        or ""
    ).strip()
    if attested and live_name and not _names_match(live_name, attested):
        return ProvenanceResult.MISMATCH

    return ProvenanceResult.MATCH


def fetch_live_company_info(mapping):
    """Fetch CompanyInfo for the mapping's realm.

    Returns the CompanyInfo dict, or ``None`` when it cannot be fetched. A
    ``None`` propagates to ``UNKNOWN`` in :func:`verify_company_provenance`,
    which is non-persistible by contract.
    """
    try:
        client = QBOApiClient(mapping.realm_id)
        info = client.get_company_info()
    except Exception:  # noqa: BLE001 — an unprobeable realm is UNKNOWN
        return None
    return info.get("CompanyInfo") or {}


def provenance_error_message(mapping, result):
    """Human-readable rejection reason for logs and operator surfaces."""
    if result is ProvenanceResult.MISMATCH:
        return _(
            "Import rejected: QuickBooks data does not belong to company "
            "'%(company)s' on realm %(realm)s."
        ) % {"company": mapping.company_id.display_name, "realm": mapping.realm_id.realm_id}
    return _(
        "Import rejected: QuickBooks ownership could not be established for "
        "company '%(company)s'."
    ) % {"company": mapping.company_id.display_name}


def _names_match(left, right):
    from ..models.qbo_realm import company_names_match  # noqa: PLC0415

    return company_names_match(left, right)
