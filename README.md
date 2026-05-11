# FrappeAI Assistant

FrappeAI Assistant is an AI assistant for Frappe and ERPNext with live ERP reads as the primary structured-data path. The app keeps legacy LanceDB-backed vector indexing as an optional compatibility layer for `assistant_mode = v1` and future document-oriented sources.

## Legacy Internal Names

This release candidate keeps several compatibility names unchanged on purpose:

- Python package and Frappe app: `frapperag`
- Desk routes: `/rag-chat`, `/rag-admin`, `/rag-health`
- Role records: `RAG Admin`, `RAG User`
- Internal DocType names such as `RAG System Health`

Public branding is now `FrappeAI Assistant`, but those internal names remain in place for RC stability.

## Highlights

- Public product name: `FrappeAI Assistant`
- Primary structured path: live ERP querying through `assistant_mode = hybrid`
- Optional legacy vector path: LanceDB + sidecar-backed compatibility indexing for `assistant_mode = v1`
- Optional local embeddings: `e5-small` for no embedding egress
- Safe-by-default scope: read-only ERP access, allowlisted reports, bounded analytics, and no write actions

## Requirements

| Dependency | Version |
|---|---|
| Python | 3.11+ |
| Frappe | v15+ |
| ERPNext | v15+ |
| fastapi | >= 0.110.0 |
| uvicorn | >= 0.29.0 |
| httpx | >= 0.27.0 |
| google-genai | >= 1.0.0 |

## Quickstart

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/Al-Aswany/FrappeAI-Assistant
./env/bin/pip install -r apps/frapperag/frapperag/requirements.txt
bench --site <site> install-app frapperag
bench --site <site> migrate
```

Then open **AI Assistant Settings** in Desk, add your Gemini API key, keep `Assistant Mode = v1` or switch to `hybrid`, and start the sidecar with `bench start` in development or your normal supervisor flow in production.

## Minimal Install Guide

The base install is intentionally lightweight and does not require `lancedb`, `pyarrow`, `sentence-transformers`, or `torch`.

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/Al-Aswany/FrappeAI-Assistant
./env/bin/pip install -r apps/frapperag/frapperag/requirements.txt
bench --site <site> install-app frapperag
bench --site <site> migrate
```

What this gives you:

- Desk pages and settings
- Sidecar chat runtime
- `assistant_mode = hybrid` structured reads
- Graceful legacy-vector unavailable behavior when optional vector dependencies are absent

What this does not install:

- `lancedb`
- `pyarrow`
- `sentence-transformers`
- `torch`

## Optional Legacy-Vector Install Guide

Install this only if you want legacy/manual LanceDB indexing, legacy vector retrieval for `v1`, or manual compatibility reindexing from **Legacy Vector Index Manager**.

```bash
./env/bin/pip install -r apps/frapperag/frapperag/requirements-legacy-vector.txt
```

Editable installs with extras are also supported:

```bash
./env/bin/pip install -e apps/frapperag
./env/bin/pip install -e 'apps/frapperag[legacy-vector]'
./env/bin/pip install -e 'apps/frapperag[legacy-vector,local-embeddings]'
```

## Optional Local-Embeddings Install Guide

Install this only if you want the `e5-small` local embedding provider. CPU-only PyTorch must be installed first.

```bash
./env/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
./env/bin/pip install -r apps/frapperag/frapperag/requirements-local-embeddings.txt
```

Omitting the first step causes pip to choose the default CUDA wheel, which is unnecessary on CPU-only servers.

## Configuration

1. Open **AI Assistant Settings**.
2. Enter your **Gemini API Key**.
3. Review **Allowed ERP DocTypes**. Defaults are seeded on install and migrate.
4. Review **Allowed Roles**. Internal role names remain `RAG Admin` and `RAG User` in this RC.
5. Optionally add **Allowed AI Reports**.
6. Optionally add **Queryable Fields / Aggregate Policy** rows.
7. Keep **Enable Legacy Transactional Vector Sync** off unless you explicitly want legacy per-record vector sync.
8. Change **Embedding Provider** only if you have installed the optional dependencies needed for that provider.

## Architecture

```text
Frappe Desk + workers
        |
        | frappe.call / background jobs / httpx
        v
FrappeAI Assistant sidecar (FastAPI on localhost)
        |
        +-- /chat   -> Gemini runtime
        +-- /health -> runtime capability status
        +-- /search -> optional legacy vector retrieval
        +-- /upsert -> optional legacy vector indexing
        |
        v
bench-level rag/ LanceDB storage (optional)
```

Runtime split:

- Frappe workers own chat/session records, policy, permissions, hybrid execution, and background jobs.
- The sidecar owns Gemini chat transport, embedding provider loading, optional vector operations, and health reporting.
- `assistant_mode = hybrid` uses live ERP reads for structured questions.
- `assistant_mode = v1` can still rely on legacy/manual vector compatibility when optional vector dependencies are installed.

## Installation Behavior

`after_install` automatically:

1. Creates the bench-level `rag/` directory for optional LanceDB data.
2. Appends a `rag_sidecar:` entry to the bench `Procfile`.

`after_migrate` automatically:

1. Reasserts sidecar process-manager entries.
2. Seeds default `Allowed ERP DocTypes`, roles, reports, and aggregate-policy rows.
3. Keeps `Enable Legacy Transactional Vector Sync = 0` unless already changed on the site.

## Desk Surfaces

- `/rag-chat` -> **AI Assistant Chat**
- `/rag-admin` -> **Legacy Vector Index Manager**
- `/rag-health` -> **AI Assistant Health**

The route names stay unchanged for compatibility even though the visible titles are rebranded.

## Legacy Vector Operations

### Manual indexing

Navigate to **Legacy Vector Index Manager** (`/rag-admin`) in Desk, choose a DocType, and click **Start Legacy Indexing**. This remains a manual compatibility tool, not the primary structured-data path.

### Transactional vector sync

Legacy transactional vector sync is disabled by default. When **Enable Legacy Transactional Vector Sync** is off, normal ERP saves, renames, and deletes do not enqueue legacy vector jobs.

If you explicitly enable it, the legacy sync flow resumes for the supported ERP DocTypes and the **Legacy Vector Sync Health** panel in **AI Assistant Settings** continues to expose failures and retries.

## Troubleshooting

### `/health` is reachable but vectors are unavailable

Expected on a minimal install. The sidecar should still report chat availability while `vector_available = false` until you install `requirements-legacy-vector.txt`.

### `e5-small` is selected but embeddings do not work

Install both:

- `torch --index-url https://download.pytorch.org/whl/cpu`
- `requirements-local-embeddings.txt`

Then restart the sidecar.

### `v1` responses have no citations on a minimal install

Expected when legacy vector dependencies are not installed. The app should degrade gracefully instead of crashing.

### Manual legacy indexing says unavailable

Expected on a minimal install without `lancedb` and `pyarrow`. Install `requirements-legacy-vector.txt` and restart the sidecar.

### Desk routes still start with `/rag-`

Intentional in this RC. The visible product name is rebranded, but internal Desk routes stay stable for compatibility.

## Demo Prompts

Try these after configuring the app:

- `Show the latest 10 Sales Invoices with customer and grand total.`
- `Which customers generated the highest sales this month?`
- `Summarize recent purchase activity for supplier ACME Supplies.`
- `Which allowed reports can help me review receivables?`
- `What is the current sidecar and vector capability status?`

## Security And Safety

- Structured ERP access is read-only in this RC.
- No write actions are exposed through chat.
- Report execution is allowlisted.
- Aggregate/queryable fields are policy-backed.
- Role gating still relies on the internal `RAG Admin` and `RAG User` role names.
- Gemini API usage still sends chat prompts to Google.
- Embedding egress depends on provider:
  - `gemini`: indexed text is sent to Google for embedding
  - `e5-small`: embedding stays local after the model is installed
- File/image support, Text-to-SQL, WhatsApp/Telegram, and LLM Wiki are out of scope for this RC.

## Final Smoke Matrix

| Check | Status | Notes |
|---|---|---|
| Phase 7A lightweight install runner on existing site | Passed | Existing-site runner passed 7/7 before rebrand |
| Phase 7B existing-site rebrand smoke | Verified | `migrate`, `clear-cache`, page-title probes, Phase 7A runner, Phase 6 cleanup runner, Phase 4E hybrid runner, and Phase 4D analytics runner passed on the existing site |
| Phase 7C fresh minimal install | Deferred | New bench verification remains intentionally deferred until after rebrand |
| Optional legacy-vector restore on fresh bench | Deferred | Must confirm vector capability returns after optional dependency install |

## Manual Verification Commands

```bash
bench --site golive.site1 migrate
bench --site golive.site1 clear-cache
bench --site golive.site1 execute frapperag.tests.phase7a_lightweight_install_runner.run_matrix
bench --site golive.site1 execute frapperag.tests.phase6_cleanup_runner.run_matrix
bench --site golive.site1 execute frapperag.tests.phase4e_hybrid_runner.run_matrix
bench --site golive.site1 execute frapperag.tests.phase4d_analytics_runner.run_matrix
bench --site golive.site1 execute frapperag.api.health.get_health_status
bench --site golive.site1 execute frapperag.api.local_model.get_active_prefix_status
```

## Repository Notes

- GitHub repo: `https://github.com/Al-Aswany/FrappeAI-Assistant`
- Internal package name for this RC: `frapperag`
- Optional requirements files:
  - `apps/frapperag/frapperag/requirements.txt`
  - `apps/frapperag/frapperag/requirements-legacy-vector.txt`
  - `apps/frapperag/frapperag/requirements-local-embeddings.txt`
  - `apps/frapperag/frapperag/requirements-documents.txt`
