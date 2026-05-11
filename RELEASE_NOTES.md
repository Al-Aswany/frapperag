# FrappeAI Assistant Release Notes

## RC Summary

This release candidate rebrands the product publicly as `FrappeAI Assistant` while intentionally preserving the current internal package, route, role, and DocType identifiers for stability.

## Scope

- Included:
  - Public branding update
  - Desk title cleanup
  - README and release-document rewrite
  - Existing-site smoke verification
- Explicitly excluded:
  - Full package rename
  - Role-record rename
  - Desk route rename
  - Text-to-SQL
  - Write actions
  - File/image support
  - WhatsApp/Telegram
  - LLM Wiki

## Install Variants

- Minimal install:
  - `requirements.txt`
  - No `lancedb`, `pyarrow`, `sentence-transformers`, or `torch`
- Optional legacy vector compatibility:
  - `requirements-legacy-vector.txt`
- Optional local embeddings:
  - CPU `torch`
  - `requirements-local-embeddings.txt`

## Known Limits

- Internal RC package/app name remains `frapperag`.
- Internal Desk routes remain `/rag-chat`, `/rag-admin`, and `/rag-health`.
- Internal roles remain `RAG Admin` and `RAG User`.
- Internal DocType names remain `RAG*` where already established.

## Smoke Matrix Status

| Check | Status | Notes |
|---|---|---|
| Existing-site lightweight install runner | Passed previously | Phase 7A runner passed 7/7 before rebrand |
| Existing-site rebrand smoke | Verified | Existing-site migrate, cache clear, metadata probes, and runner suite passed after the rebrand updates |
| Fresh minimal install | Deferred | Phase 7C |
| Optional legacy-vector restore | Deferred | Phase 7C |

## Phase 7C Checklist

1. Create a new bench.
2. Install from `https://github.com/Al-Aswany/FrappeAI-Assistant`.
3. Install core/default dependencies only.
4. Confirm no vector/local-embedding extras are present.
5. Install app and migrate.
6. Start sidecar and verify `/health`.
7. Verify hybrid `get_list`.
8. Verify hybrid analytics.
9. Verify `v1` graceful unavailable behavior.
10. Verify manual legacy indexing unavailable behavior.
11. Install `requirements-legacy-vector.txt` and verify vector capability returns.
