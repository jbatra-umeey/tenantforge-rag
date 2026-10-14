# Validation evidence — 2026-10-14

## Result and scope

**The local executable path passed functional, isolation, provider-contract, parser-sandbox and live HTTP checks.** PostgreSQL, live model services, real OIDC/S3, Docker/Kubernetes and large-scale capacity remain unverified in this environment.

Runtime: Python 3.12.14 on Linux; JavaScript syntax checked with the installed Node runtime. Resolved Python versions are recorded in `requirements-tested.txt`. This is a validation snapshot, not a security certification.

| Check | Observed result |
|---|---|
| Python test suite | **57 passed, 5 skipped**, one dependency warning |
| Statement coverage | **81%** across `tenantforge`; excludes subprocess/live-server execution from that coverage measure |
| Python lint | `ruff check .` passed |
| Python bytecode compilation | Passed for application and scripts |
| Browser JavaScript syntax | `node --check` passed for dashboard and browser-check script |
| Synthetic retrieval/answer regressions | **12/12 expected answer matches; 12/12 expected source/abstention checks** |
| Real HTTP + running ingestion worker | Five formats uploaded and indexed; grounded answer, citation endpoints and stored conversation verified |
| Small repeated-query HTTP probe | **20/20 HTTP 200**, concurrency 4, local hash/extractive profile |
| Compose/Kubernetes/Prometheus/CI YAML | YAML documents parsed successfully; no runtime or Kubernetes schema certification |
| Actual browser UI automation | **Not completed**: Chromium absent; browser download failed with an invalid/truncated archive |
| PostgreSQL/pgvector integration | **5 tests skipped**: real database/roles unavailable |
| Docker image/Compose deployment | **Not run**: Docker unavailable |
| Kubernetes deployment and HPA | **Not run**: templates only |
| Live paid embedding/generation calls | **Not run**: HTTP contracts tested with mocked transport |
| Neural local embeddings/cross encoder | **Not run**: optional weights/dependencies not installed |
| Real OIDC and S3 integration | **Not run**: local signature tests and adapters do not prove external deployment behavior |
| 100K users, HA and disaster recovery | **Not tested**: design and qualification plan supplied |

## What the tests actually exercise

- Upload, parsing, indexing, answering and citation retrieval for PDF, DOCX, HTML, CSV and plain text.
- Contradictory tenant facts, document/source/delete isolation and user-private conversation access.
- Reader/editor access, token tampering/expiry, signed tenant identity, rejected body overrides and unknown tenants.
- Metadata filtering, cache reuse/separation, document deletion, stale-corpus checks and embedding-fingerprint enforcement.
- Queue durability across worker instances, lease reclaim, old-worker fencing, deletion during processing, dead-letter and retry limits.
- Atomic budget reservations under concurrent requests, admission limits, input validation and request size checks.
- Real parser subprocess success/failure, DOCX expansion bounds and extracted-text/chunk limits.
- Embedding API batching/order/dimension contract; model-route/citation validation; transient retry and sanitized errors.
- OIDC RSA signature and claim validation using a generated test key and controlled JWKS object. This is not a live issuer test.

Most integration tests invoke the same parser synchronously to keep the suite fast; two tests execute the actual isolated subprocess. The real HTTP check also runs the subprocess parser through a live worker.

## Local probe results

The latest small probe used 20 repeated questions, concurrency 4, five sample documents, one SQLite API process and a worker on the same host. It used **hash embeddings, extractive output and eligible answer-cache reuse**, not an external LLM.

| Measurement | Observed |
|---|---:|
| Successful responses | 20/20 |
| Elapsed | ~0.20 s |
| Aggregate throughput | ~99.36 requests/s |
| p50 request latency | 23.69 ms |
| p95 request latency | 88.21 ms |
| p99 request latency | 133.73 ms |

This tiny closed-loop probe is only a functional concurrency smoke test. It is too short and too small to establish sustained throughput, production SLOs or 100K-user capability. Do not compare it to an uncached remote-model workload. The script reports errors and throttles if present; it does not silently count them as successes.

## Corrected during validation

DOCX headings were initially indexed as standalone passages and could become weak citations. They now stay attached to following content. The extractive baseline suppresses weaker overlapping passages relative to the best match. Follow-up retrieval can use prior question context, while answering uses the actual current question; a regression verifies the “When does it begin?” follow-up resolves the January leave-year fact.

Provider calls support proxy environments through the HTTP client’s SOCKS extra. Worker cache writes are fenced in the same transaction as lease validation, and publication rejects expired/cancelled work.

## Evidence files

`docs/validation/` contains the test/coverage log, synthetic evaluation JSON, HTTP smoke JSON and small probe JSON. No tokens, user uploads, credentials or server logs are included. Browser screenshots are not provided because browser execution did not complete.

## Reproduce and extend

```bash
pip install -e '.[dev]'
ruff check .
pytest -q --cov=tenantforge --cov-report=term-missing
python scripts/evaluate.py
python scripts/verify_local.py
```

To run pending browser checks, install the optional Node dependencies and Chromium, then use `python scripts/verify_local.py --browser`. To run PostgreSQL checks, use the three URLs documented in the README and a real pgvector service. CI includes that service but no remote CI run was performed here.

Before describing a deployment as production-ready, execute the missing infrastructure/security/model/load/recovery gates with the actual deployed versions and record their outputs. No repository was published and no cloud service was deployed as part of this build.
