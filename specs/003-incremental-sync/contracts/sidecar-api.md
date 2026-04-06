# RAG Sidecar HTTP API Contract

**Sidecar**: FastAPI + uvicorn, `localhost:{sidecar_port}` only
**Table prefix**: `v3_`
**Record ID format**: `{DocType}:{name}` (e.g. `Customer:CUST-0001`)
**Table name format**: `v3_` + doctype.lower().replace(" ", "_") (e.g. `v3_sales_invoice`)

All requests and responses use `Content-Type: application/json`. No authentication header — localhost-only (Constitution Principle IV).

---

## GET /health

Liveness check. Returns 200 when the sidecar is ready to serve requests.

**Response 200**:
```json
{ "status": "ok", "model": "multilingual-e5-base" }
```

---

## POST /embed

Embed a list of texts using `multilingual-e5-base`. Returns 768-dim float vectors in input order.

**Request body**:
```json
{
  "texts": ["text one", "text two"]
}
```

**Response 200**:
```json
{
  "vectors": [
    [0.012, -0.034, ...],   // 768 floats
    [0.056,  0.078, ...]
  ]
}
```

**Response 422** (validation error): `texts` is empty or not a list of strings.

**Response 500**: Model inference failed.

---

## POST /upsert

Embed one record's text and upsert its vector into the appropriate `v3_` LanceDB table. Creates the table if it doesn't exist.

**Request body**:
```json
{
  "doctype": "Customer",
  "name":    "CUST-0001",
  "text":    "Customer: CUST-0001 — Acme Corp, credit limit 50000..."
}
```

**Response 200**:
```json
{ "ok": true }
```

**Response 422**: Missing required fields.

**Response 500**: Embedding or LanceDB write failed.

**Implementation note**: The sidecar computes `table = "v3_" + doctype.lower().replace(" ", "_")` and `id = f"{doctype}:{name}"`, then calls `merge_insert("id")` so existing entries are updated and new entries are inserted.

---

## DELETE /record/{table}/{record_id}

Remove one vector entry from a LanceDB table by composite ID. No-op if the record does not exist.

**Path parameters**:
- `table` — URL-encoded table name, e.g. `v3_customer`
- `record_id` — URL-encoded composite ID, e.g. `Customer%3ACUST-0001`

**Response 200**:
```json
{ "ok": true, "found": true }
```
`found: false` when the record did not exist (still 200 — idempotent).

**Response 500**: LanceDB delete failed.

---

## DELETE /table/{table}

Drop an entire LanceDB table. Used when a DocType is removed from the whitelist (FR-005). Idempotent — no-op if the table does not exist.

**Path parameter**:
- `table` — table name, e.g. `v3_customer`

**Response 200**:
```json
{ "ok": true, "existed": true }
```
`existed: false` when the table was not found (still 200 — idempotent).

**Response 500**: LanceDB drop failed.

---

## Error format (all 4xx/5xx)

```json
{
  "detail": "Human-readable error description"
}
```
