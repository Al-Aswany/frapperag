"""Deprecated compatibility shim for legacy structured-record vector helpers.

Phase 6 moved the canonical policy logic to ``legacy_vector_policy.py``.
Keep these exports stable so existing imports, tests, and patches continue to
work without renaming public/internal call sites all at once.
"""

from __future__ import annotations

from frapperag.rag.legacy_vector_policy import (
    LEGACY_VECTOR_DOCTYPES,
    LEGACY_VECTOR_DOCTYPE_SET,
    is_legacy_auto_sync_enabled,
    is_legacy_vector_doctype,
)

TRANSACTIONAL_VECTOR_DOCTYPES: tuple[str, ...] = LEGACY_VECTOR_DOCTYPES
TRANSACTIONAL_VECTOR_DOCTYPE_SET = LEGACY_VECTOR_DOCTYPE_SET


def is_transactional_vector_doctype(doctype: str | None) -> bool:
    return is_legacy_vector_doctype(doctype)


def is_transactional_vector_sync_enabled(settings=None) -> bool:
    return bool(getattr(settings, "enable_transactional_vector_sync", 0))


def is_transactional_vector_sync_allowed(settings=None, doctype: str | None = None) -> bool:
    return is_legacy_auto_sync_enabled(settings, doctype)
