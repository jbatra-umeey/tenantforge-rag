# Security design and boundaries

## Access model

Documents are visible to all authenticated members of their organization. Reader tokens can search and read; editor/admin tokens can upload, delete and retry organization documents. Conversations and answer caches are private to the `(tenant_id, user_id)` pair. There is no document-level private-user ACL, group ACL, tenant invitation UI or entitlement service in this version.

Tenant membership must be issued by a trusted identity provider. The local signing key is public demo material. Never deploy local auth with the default key to a shared network. Use production mode, a real IdP and short token lifetimes for deployment.

## Threats and implemented controls

| Threat | Control | Residual risk / next step |
|---|---|---|
| Tenant ID spoofing | Signed claims only; body extras rejected; untrusted header ignored | A compromised trusted issuer/application can still forge context |
| Missing tenant predicate | Explicit predicates plus forced PostgreSQL RLS | Real PostgreSQL policies must be run/tested in target infrastructure |
| Private memory leakage | Subject+tenant checks, user RLS, ownership checks on reads/writes/deletes | User mapping and IdP organization claims must be correct |
| Cache leakage | Tenant/user/revision/model/filters/history in cache keys | Old physical rows remain until cleanup; no global cache sharing |
| Object path traversal | UUID keys, verified prefix and path normalization | S3 IAM must prohibit public access and constrain server identities |
| Parser abuse | Input/output/ZIP/page/chunk bounds, child process, wall/CPU/memory limits | This is resource isolation, not a hardened VM or malware scanner |
| HTML/script injection | Remove active elements; browser displays answers/sources through `textContent`; CSP | Third-party API docs use a narrower route-specific CDN/inline policy |
| Prompt injection in documents | Sources labeled untrusted; evidence-only prompt; no autonomous tools | A prompt instruction is not a complete prompt-injection defense |
| Fake citations | Source IDs assigned by service and validated against current result set | Correct IDs do not guarantee claim support; evaluate entailment |
| Replay or stale publication | Expiring JWTs, lease/fence checks, revision check | Token revocation is via IdP/lifetime, not a local revocation database |
| Resource exhaustion | Per-tenant quota/admission controls, bounded contexts/timeouts and queues | Need edge limits, noisy-neighbor scheduling and provider circuit breakers |
| Credential exposure | Runtime env/secret config, sanitized errors, no prompt/body logs | Operator logs, process environments and deployment secret access need controls |

## Defense-in-depth details

PostgreSQL `FORCE ROW LEVEL SECURITY` does not constrain a superuser or a BYPASSRLS role. Use a separate migration identity; the application role is a non-owner. The application checks this in production readiness. `SET LOCAL`-equivalent settings are established for every database transaction, including pooled-connection reuse. Rows are not protected from an attacker who has arbitrary access to the privileged migration credentials.

The worker can discover jobs across tenants because it is a trusted dispatcher/processor. Its special policy applies only to jobs. Other content tables remain tenant scoped, and access to conversations/query caches is revoked from the worker role. Application roles should not be permitted to assume the worker role.

The S3 adapter uses private API access and requests server-side AES256 encryption; it does not return public URLs. Bucket policy, block-public-access, TLS, KMS requirements, per-tenant access points and workload identities are operator-managed. A shared service credential plus a prefix check is not cryptographic tenant separation.

The API never fetches user-supplied remote HTML URLs. Uploaded HTML is parsed without fetching linked resources. File extensions alone are not a malware boundary; PDFs and DOCX receive structural validation, and the parser remains a dependency trust boundary.

## Checks included

The tests cover tenant-conflicting answers, cross-tenant source reads/deletes, private conversation reads/writes/deletes, forged/expired tokens, body-tenant overrides, reader permissions, unknown tenants, metadata filtering, cache separation, deletion races, old worker fences, quota races and parser expansion limits. OIDC signature/claim behavior is checked with a generated test RSA key and mocked JWKS retrieval.

PostgreSQL integration tests deliberately omit application `WHERE` clauses, attempt cross-tenant writes and try to disable RLS using the actual application role. These tests are skipped without a real pgvector service; a test file is not proof that a deployment passed it.

## Before external exposure

Run the real OIDC/S3/PG tests, threat-model the deployment, scan pinned dependencies/images, test malformed uploads, add edge TLS/body/concurrency limits, and define data retention/erasure. Add per-document/group authorization if the product requires it. Arrange a security review for high-sensitivity workloads. The repository is not security-certified or represented as production-ready.
