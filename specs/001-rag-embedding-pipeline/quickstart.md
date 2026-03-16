# Quickstart: RAG Embedding Pipeline — Phase 1

**Branch**: `001-rag-embedding-pipeline`
**Date**: 2026-03-15
**Prerequisites**: A running Frappe v15 bench with ERPNext v15 installed.

---

## Step 1: Install the app

```bash
# From the bench root
bench get-app https://github.com/your-org/frapperag
bench --site your-site.localhost install-app frapperag
bench --site your-site.localhost migrate
bench restart
```

No additional setup is required. The `after_install` hook creates
`{site}/private/files/rag/` automatically.

---

## Step 2: Configure AI Assistant Settings

1. Open **ERPNext → FrappeRAG → AI Assistant Settings**
   (or search "AI Assistant Settings" in the Desk search bar).
2. Set **Enabled** to ✅.
3. Enter your **Gemini API Key** (from Google AI Studio or Google Cloud Console).
4. In **Allowed Document Types**, add:
   - `Sales Invoice`
   - `Customer`
   - `Item`
5. In **Allowed Roles**, add the roles that should be able to trigger indexing
   (e.g., `System Manager`, `RAG Admin`).
6. Set **Sync Schedule Preference** to `Manual Only` (default).
7. Click **Save**.

---

## Step 3: Trigger an Indexing Job

**Via the Frappe Desk UI (admin page)**:

1. Open **FrappeRAG → Index Documents** (or the RAG admin page).
2. Select the document type (e.g., `Customer`).
3. Click **Start Indexing**.
4. A job ID appears immediately (e.g., `AI-INDX-2026-03-15-0001`).
5. A live progress bar updates as documents are embedded.

**Via browser console (for testing)**:

```javascript
frappe.call({
    method: "frapperag.api.indexer.trigger_indexing",
    args: { doctype: "Customer" },
    callback: function(r) {
        console.log("Job started:", r.message.job_id);
    }
});
```

---

## Step 4: Monitor the Job

The progress bar on the admin page updates automatically via WebSocket.

**To check status manually**:

```javascript
frappe.call({
    method: "frapperag.api.indexer.get_job_status",
    args: { job_id: "AI-INDX-2026-03-15-0001" },
    callback: function(r) {
        console.log(r.message);
        // { status: "Running", progress_percent: 45, processed_records: 90, ... }
    }
});
```

---

## Step 5: Verify the Index Was Written

After the job status shows `Completed`, verify the LanceDB files exist:

```bash
ls -la sites/your-site.localhost/private/files/rag/
# Should show: customer.lance/ (or similar)

# Count indexed records using the bench Python console
bench --site your-site.localhost console
```

```python
import lancedb, frappe
db = lancedb.connect(frappe.get_site_path("private", "files", "rag"))
tbl = db.open_table("v1_customer")
print(f"Indexed records: {tbl.count_rows()}")
print(tbl.to_pandas()[["id", "name", "text"]].head())
```

Expected output: One row per Customer record, with the `text` column showing
a human-readable summary and the `vector` column containing 768 floats.

---

## Acceptance Validation Checklist

Work through these manually after install:

- [ ] **US1-1**: Fresh install — settings form opens with all fields empty.
- [ ] **US1-2**: Enter API key, save — key is masked (not shown in plain text).
- [ ] **US1-3**: Select Customer, save — only Customer appears as indexable in UI.
- [ ] **US1-4**: Remove all roles, try to trigger — system rejects with permission error.
- [ ] **US2-1**: Trigger Customer indexing — job ID returned in under 3 seconds.
- [ ] **US2-2**: Log in as non-admin user, trigger — rejected with permission error.
- [ ] **US2-3**: Trigger Customer while one is running — second attempt rejected.
- [ ] **US2-4**: Index a DocType with zero records — job completes immediately, status Completed.
- [ ] **US3-1**: Watch progress bar with 50+ records — updates at least twice before completion.
- [ ] **US3-2**: Let job complete — status shows "Completed", no further updates.
- [ ] **US3-3**: Manually corrupt a single record to force per-doc error — status shows "Completed with Errors".
- [ ] **US3-4**: Revoke API key mid-job — status shows "Failed" with error message.
- [ ] **US4-1**: Run 3 jobs (Sales Invoice, Customer, Item) — all appear in history list.
- [ ] **US4-2**: Open a failed job — error message and timestamp visible.

---

## Troubleshooting

### Job stays in "Queued" status

The Frappe worker is not running. Start it:

```bash
bench worker --queue long &
```

Or ensure `bench start` is running and includes worker processes.

### `ResourceExhausted` errors in Error Log

The Gemini API free tier rate limit has been hit. Options:
- Wait and re-trigger (the job retries each batch up to 3 times with back-off).
- Switch to a paid Gemini API key (higher rate limits).

### LanceDB directory not found

The `after_install` hook did not run. Create it manually:

```bash
mkdir -p sites/your-site.localhost/private/files/rag
```

Then re-run the install hook:

```bash
bench --site your-site.localhost execute frapperag.setup.install.after_install
```

### Job shows "Failed (Stalled)"

The background worker crashed mid-job. Check `frappe.log_error` entries for
the job ID. Re-trigger the indexing job — it will re-index all records from
scratch (Phase 1 does full re-index; incremental delta is Phase 2).
