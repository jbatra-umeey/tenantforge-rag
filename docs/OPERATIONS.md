# Operations and reliability notes

## Boot and identity

Run schema initialization explicitly with a migration identity. Do not give the API table ownership, superuser privileges or BYPASSRLS. `TF_ENVIRONMENT=production` requires PostgreSQL, OIDC, S3, a metrics credential and a non-hash embedding backend. `readyz` checks DB reachability, vector dimensionality and production RLS/role invariants. It does not certify the IdP, storage, provider, network or disaster-recovery setup.

OIDC tokens must contain `sub`, `tenant_id`, `role`, `iat`, `exp`, `iss` and `aud`. Configure issuer-side claim mapping to validated organization membership. Allowed roles are reader, editor and admin; admin currently has the same application permissions as editor. An IdP administrative role is not inferred from arbitrary token claims. The trusted issuer decides membership; clients cannot self-enroll an organization through this API.

JWKS is fetched from the configured HTTPS endpoint, not from a URL carried in the token. The algorithm is RS256 for OIDC and HS256 only in local mode. Keys and issuer rotation need live staging tests. Tokens are never accepted from query strings.

Provision a tenant using a trusted CLI identity:

```bash
python -m tenantforge init --tenant acme
```

`init` performs schema setup and tenant provisioning. For future schema changes, introduce versioned migrations before using this against a populated service. The current initializer handles a fresh schema and idempotent index/policy creation; it is not an upgrade framework.

## Process model

| Process | Work | Scaling |
|---|---|---|
| API | Authenticate, accept uploads, synchronous questions, source/history access | Multiple stateless replicas with shared PostgreSQL and S3 |
| Worker | Claim SQL jobs, parse, embed, publish | Multiple independent workers; PostgreSQL skips locked queue rows |
| Parser subprocess | One bounded document parse | Spawned per job; timeout/terminate on overrun |
| Migration/maintenance | Schema, tenant setup, retention cleanup | Trusted, deliberately scheduled jobs |

The local `demo` command runs an API plus a worker thread under one process. It is a convenience launcher, not the production process model. Production containers run as UID 10001 with an API probe; Kubernetes templates use read-only roots and temporary volumes. The supplied manifests need your image, IdP, S3, IAM, secrets, networking and TLS setup.

## Retry and failure handling

The default worker lease is 120 s. Parsing is bounded to 30 s wall time and 20 s CPU where supported. Provider HTTP timeouts are 25 s with up to two attempts. Leases renew around embedding batches. Slow local model initialization can exceed a lease; pre-warm models and size leases from measurements.

Transient job errors requeue with bounded exponential backoff and jitter. Invalid document/configuration errors become dead letters. Three job attempts are allowed by default; an expired final attempt is eventually marked dead on reclaim. A reclaimed job receives a new fence, preventing the previous worker from publishing stale work. Provider duplicate charges remain possible after ambiguous failures.

`POST /v1/documents/{id}/retry` resets a dead-letter job after an editor corrects the environment. A corrupt document cannot be repaired by retries; delete it and upload a valid source.

| Signal | First diagnostic action |
|---|---|
| Documents remain queued | Check worker process, DB role, queue availability and pending limits |
| Repeated failed parsing | Confirm format, unencrypted PDF, extractable text and input limits |
| `502` generation errors | Check model contract, credentials and provider availability; exceptions are sanitized |
| `409` corpus changed | Retry the question; ingestion/deletion invalidated the captured version |
| `409` conversation busy | Wait for active request or lease expiry; do not share a conversation across users |
| `429` | Check minute rate, daily reserved units, pending jobs and document cap |
| Missing filtered results | Evaluate ANN filtered recall and metadata values; inspect query plan |

## Deletion, retention and privacy

Document deletion cancels its jobs, tombstones the metadata, deletes chunks, clears tenant embedding cache and increments corpus version in one SQL transaction. Private answer caches under previous corpus versions become unreachable immediately and expire later. The deleting user’s answer-cache rows are removed immediately; other users’ rows remain subject to their ownership policy and TTL. A worker cannot republish a tombstoned document.

Raw object deletion occurs after revocation. If it fails, the response includes `object_cleanup_pending=true`. `purge` retries tombstoned object removal. This design prioritizes immediate authorization/retrieval revocation while acknowledging eventual byte cleanup.

Conversation text may already contain previously returned excerpts. History from an old corpus version is excluded from new prompts, but the user can still inspect their old stored transcript until deleting it. `DELETE /v1/conversations/{id}` removes that user’s transcript. This is not a complete organization-wide erasure workflow. Backups, object versions, audit retention and IdP records need an explicit policy before regulated use.

Run scoped cleanup with an authorized identity:

```bash
python -m tenantforge purge --tenant acme --user alice
```

This retries deleted raw objects, removes that user’s expired answer caches, removes tenant embedding cache entries older than seven days, and expires old quota buckets. It does not scan orphan objects or delete audit records. A production scheduler must iterate provisioned tenants/users or use a narrowly authorized maintenance role. No scheduler is silently running in the app.

Storage and SQL cannot commit together. Reconcile unreferenced storage objects after a safe grace interval; do not delete recently uploaded objects that may still be in-flight. Local/S3 object listing, a reconciliation ledger and an automated orphan sweeper remain follow-up implementation work.

## Observability

API metrics at `/metrics` include route/status request counts, route latency histograms, stage latency, job outcomes visible in that process and cache outcomes. Use one API process per container and scrape every replica. Worker metrics are on internal port 9101; do not expose that endpoint publicly. Worker outcomes must be scraped from worker processes, not assumed to appear in API metrics.

Metric labels omit tenant, user, prompt and document IDs to avoid sensitive data and unbounded cardinality. Request logs contain request ID, route template, status and elapsed time. Audit events record tenant, user, action and resource ID in tenant-scoped SQL. They do not store raw prompts or model/provider error bodies.

Set `TF_OTLP_ENDPOINT` to enable batched OTLP/HTTP span export. Spans cover HTTP, ingestion, retrieval and generation. They intentionally exclude content. Incoming W3C context propagation, full SQL instrumentation and a collector deployment are not included; add and verify them if required.

`infra/prometheus-alerts.yaml` includes provisional error-rate, p95 and dead-letter alerts. Tune from real workload baselines. Add pool/lock wait, provider quota, oldest queued job, ingestion throughput and retrieval-quality dashboards before launch.

## Recovery and rollout

- Use PostgreSQL backups/PITR and test restoration. Raw source objects need a compatible backup/versioning policy. Vector indexes can be rebuilt, but lost source or conversation data may not be recoverable from them.
- Treat embeddings as derived data, with model/version lineage. Rebuild into a separate corpus when model, dimension or chunking semantics change.
- Use rolling API/worker deployments with backward-compatible database changes. Worker leases recover interrupted jobs; verify that shutdown and resource limits do not repeatedly kill healthy parsers.
- Run isolation tests with the real non-owner database role and real identity claims before promoting a deployment.
- Pin container digests, scan dependencies and images, configure KMS/IAM/private networking, add TLS and validate restore/failover. These are deployment gates, not work completed by the included templates.
