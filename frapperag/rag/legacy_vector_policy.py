"""Canonical policy helpers for legacy structured-record vector flows.

Phase 6 keeps the legacy vector stack for v1 compatibility and manual/admin
maintenance, but narrows its activation boundary to the fixed ERP DocType set
below plus explicit allowlist presence in AI Assistant Settings.
"""

from __future__ import annotations

from typing import Iterable


LEGACY_VECTOR_DOCTYPES: tuple[str, ...] = (
    "Customer",
    "Item",
    "Sales Invoice",
    "Purchase Invoice",
    "Sales Order",
    "Purchase Order",
    "Delivery Note",
    "Purchase Receipt",
    "Stock Entry",
    "Supplier",
    "Item Price",
)

LEGACY_VECTOR_DOCTYPE_SET = frozenset(LEGACY_VECTOR_DOCTYPES)


def is_legacy_vector_doctype(doctype: str | None) -> bool:
    return (doctype or "").strip() in LEGACY_VECTOR_DOCTYPE_SET


def _allowed_doctype_names(settings) -> set[str]:
    if not settings:
        return set()
    return {
        (getattr(row, "doctype_name", None) or "").strip()
        for row in (getattr(settings, "allowed_doctypes", None) or [])
        if (getattr(row, "doctype_name", None) or "").strip()
    }


def get_manual_indexing_targets(settings) -> list[str]:
    allowed = _allowed_doctype_names(settings)
    return [doctype for doctype in LEGACY_VECTOR_DOCTYPES if doctype in allowed]


def get_policy_only_doctypes(settings) -> list[str]:
    allowed = _allowed_doctype_names(settings)
    return sorted(doctype for doctype in allowed if doctype not in LEGACY_VECTOR_DOCTYPE_SET)


def is_legacy_auto_sync_enabled(settings, doctype: str | None) -> bool:
    if not settings or not getattr(settings, "is_enabled", 0):
        return False
    doctype_name = (doctype or "").strip()
    if not is_legacy_vector_doctype(doctype_name):
        return False
    if doctype_name not in _allowed_doctype_names(settings):
        return False
    return bool(getattr(settings, "enable_transactional_vector_sync", 0))


def is_legacy_v1_retrieval_allowed(settings, doctype: str | None) -> bool:
    if not settings or not getattr(settings, "is_enabled", 0):
        return False
    doctype_name = (doctype or "").strip()
    if not is_legacy_vector_doctype(doctype_name):
        return False
    return doctype_name in _allowed_doctype_names(settings)


def filter_legacy_vector_candidates(candidates: Iterable[dict], settings) -> list[dict]:
    allowed = []
    for candidate in candidates or []:
        if is_legacy_v1_retrieval_allowed(settings, candidate.get("doctype")):
            allowed.append(candidate)
    return allowed
