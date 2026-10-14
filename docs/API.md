# HTTP API contracts

Base URL: `http://127.0.0.1:8000`. Authenticated calls require `Authorization: Bearer <token>`. The organization/user is resolved from the token. No API field lets a client choose another tenant. Source and conversation IDs are UUIDs.

| Method and path | Purpose | Role |
|---|---|---|
| `GET /healthz` | Process health | Public |
| `GET /readyz` | DB/schema/readiness check | Public; status only |
| `GET /metrics` | Prometheus exposition | Separate metrics token when configured |
| `GET /v1/stats` | Current identity, document counts, reserved budget, modes | Reader+ |
| `POST /v1/documents` | Multipart upload and asynchronous job submission | Editor+ |
| `GET /v1/documents?offset=0&limit=50` | Paginated tenant document list | Reader+ |
| `DELETE /v1/documents/{id}` | Revoke and delete indexed source | Editor+ |
| `POST /v1/documents/{id}/retry` | Reset a dead-letter ingestion job | Editor+ |
| `POST /v1/query` | Cited answer with optional private conversation | Reader+ |
| `GET /v1/chunks/{id}` | Current authorized source text and locator | Reader+ |
| `GET /v1/conversations/{id}` | Owner’s bounded transcript | Owner |
| `DELETE /v1/conversations/{id}` | Remove owner’s transcript | Owner |
| `GET /docs` | Interactive OpenAPI contract | Public |

## Upload

`file` is the multipart binary field. `metadata` is a JSON object serialized as a form string. At most 20 keys; nonempty keys <=64 characters; values are strings <=256 characters. Filename extension must be PDF/DOCX/HTML/HTM/CSV/TXT/MD. The default source byte limit is 10 MiB plus a bounded multipart allowance for the HTTP body.

```json
{
  "document_id": "00000000-0000-0000-0000-000000000001",
  "job_id": "00000000-0000-0000-0000-000000000002",
  "status": "queued",
  "deduplicated": false
}
```

These UUIDs illustrate the response shape; they are not resources. Poll the document list until status is ready/failed. Duplicates return the existing ID and status and omit the new job ID. Failed duplicate uploads do not silently reset retries; use the retry endpoint or delete/re-upload.

## Query

```json
{
  "question": "What is the annual leave policy?",
  "conversation_id": null,
  "filters": {"department": "hr"},
  "top_k": 5,
  "quality": "standard"
}
```

Question: 1–2000 nonblank characters. `top_k`: 1–10. Filters: exact equality for top-level string metadata, ANDed together. `quality`: `standard` or `deep`. Unknown fields are rejected. Empty filters search all ready organization documents.

Response includes:

```json
{
  "answer": "Acme employees receive 25 days of annual leave per calendar year. [1]",
  "abstained": false,
  "model": "extractive-v1",
  "provider_usage": {},
  "citations": [
    {
      "number": 1,
      "chunk_id": "00000000-0000-0000-0000-000000000003",
      "document_id": "00000000-0000-0000-0000-000000000001",
      "name": "handbook.md",
      "locator": "section 2",
      "excerpt": "Acme employees receive 25 days of annual leave per calendar year.",
      "url": "/v1/chunks/00000000-0000-0000-0000-000000000003"
    }
  ],
  "retrieval_mode": "hash",
  "reranker": "lexical",
  "corpus_version": 1,
  "conversation_id": "00000000-0000-0000-0000-000000000004",
  "cache_hit": false,
  "latency_ms": 12.4
}
```

The latency above is illustrative. Real observed measurements are in the validation report. Source URLs require the same authorization checks as the rest of the API. They are not public storage links.

## Error semantics

| Status | Meaning | Caller action |
|---|---|---|
| 401 | Missing, invalid or expired token | Obtain a valid token |
| 403 | Unprovisioned tenant or insufficient role | Correct membership/role through the trusted admin path |
| 404 | Source, failed job or owned conversation unavailable | Do not infer another tenant’s resource existence |
| 409 | Embedding mismatch, concurrent conversation or changed corpus | Inspect detail; retry transient conflicts, reindex configuration changes |
| 413 | Upload/body exceeds limits or source empty | Reduce/correct input |
| 415 | Unsupported extension | Use a supported document format |
| 422 | Malformed request or metadata | Correct the request |
| 429 | Rate, budget or admission capacity reached | Respect `Retry-After`; inspect quota/backlog |
| 502 | Model provider unavailable | Retry with bounded backoff; inspect provider health |
| 503 | Local corpus limit or failed readiness | Use PostgreSQL or restore dependency health |

The response includes a request ID header. The API does not return raw parser/provider errors to clients. Generic unhandled failures return an internal error rather than exposing document contents.
