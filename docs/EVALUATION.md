# Retrieval, grounding and cost evaluation

## What the bundled evaluation measures

`python scripts/evaluate.py` creates a fresh temporary SQLite corpus, processes five actual file formats through the worker, and runs 12 synthetic questions. It checks expected answer substrings, expected cited documents and an unsupported-question abstention. Each case starts a fresh conversation, so prior answers do not supply hidden evidence.

This is a regression suite for the local pipeline. It is not a semantic-search benchmark, a blinded human judgment study or a faithfulness score. A correct expected substring does not prove that every other sentence is correct. The known dataset is small and intentionally easy to inspect.

The default hash embedder uses signed hashing of word/unigram and bigram features into a normalized 384-dimensional vector. It is deterministic and offline, not a pretrained semantic embedding model. The default reranker uses lexical coverage and RRF; it is not a cross encoder. The extractive generator returns source sentences; it is not a language model.

## A stronger evaluation harness for rollout

Build a versioned dataset containing tenant, subject, query, conversation context, metadata filters, expected document/chunk relevance, expected facts, forbidden facts and expected abstention. Include paired tenants with contradictory facts so cross-tenant leakage cannot hide behind plausible answers.

| Layer | Metric | Interpretation |
|---|---|---|
| Ingestion | Extraction success, empty-text rate, locator accuracy, chunk-length distribution | Are facts correctly represented before retrieval? |
| Retrieval | Recall@k, MRR and nDCG@k under exact/ANN search | Is the evidence available, and at what rank? |
| Filtering | Filter precision and tenant-leak count | A wrong tenant source is a security failure, not a small relevance penalty |
| Reranking | Recall retained and rank improvement versus fused baseline | Quality improvement must justify latency/cost |
| Answer | Claim-level citation support and contradiction rate | Every material factual assertion needs evidence |
| Abstention | Precision/recall for unanswerable questions | Avoid both invented answers and excessive refusal |
| Conversation | Correct follow-up resolution, user-boundary tests, stale-memory checks | Context should improve relevance without leaking or reviving deleted knowledge |
| Operations | p50/p95/p99 latency, success/throttle/error rates, cache rate and queue age | Quality and service behavior must be evaluated together |

Evaluate provider generation with human-reviewed examples and a separately calibrated judge if using LLM-based scoring. Do not make the same model its sole judge. Store actual outputs, source sets, prompt/model versions and judge disagreement. This repository does not include a calibrated judge ensemble or claim such an evaluation ran.

## Citation semantics

The application assigns numbers to supplied passages and only returns citations from that set. In model mode, no citations or out-of-range numbers cause abstention. This prevents fabricated source identities but cannot establish that a cited passage supports the claim. Prompt injection could still influence a model to cite a real passage incorrectly. Add an entailment/claim-verification stage if your reliability target requires one, then measure its false accepts and false rejects.

The default extractor ranks sentences by lexical overlap and suppresses weak matches relative to the best overlap. It can still select irrelevant passages sharing common words. That limitation is visible and preferable to pretending the offline mode provides full semantic comprehension.

## Cache experiments

Measure cold and warm paths separately. Repeated identical standalone questions from the same user can hit the answer cache. A different user cannot share that answer entry; this is a deliberate privacy/cost tradeoff. Continuing an existing conversation changes the history component of the cache key. Metadata filters, corpus version and model/prompt configuration are also included.

Deleting or publishing a source changes the corpus revision. Prior answer-cache keys become unreachable. The final revision check also protects a request already in flight. The embedding cache is tenant-local and reusable across documents with matching content/model fingerprints; no cross-organization deduplication is performed.

## Cost controls implemented

- Upload deduplication uses content + name + metadata inside one tenant.
- Embedding generation uses batches and a tenant/model/text cache.
- Queries and generation have finite text/context/output limits.
- Daily usage reservations are stored atomically, so API replicas cannot independently overspend the configured reservation counter.
- Explicit `standard` and `deep` routes choose configured models. If no deep model is set, deep uses the standard model.
- Answer cache hits skip embedding/retrieval/generation work while still recording the conversation turn and consuming a query-rate unit.

Budget units use UTF-8 byte length as a conservative input-token proxy plus output allowance and prompt overhead. They are reserved before calls, are not refunded after failures, and are also consumed in local mode to exercise enforcement. HTTP retries can cause additional billed provider usage. The counter is therefore an **admission policy**, not a financial ledger or a hard guarantee on spend. Provider response usage is returned for model generation; full provider invoice reconciliation is not implemented.

Estimate cost using your provider’s current prices and actual usage:

`generation_cost = input_tokens × input_unit_price + output_tokens × output_unit_price`

`embedding_cost = newly_embedded_tokens × embedding_unit_price`

Include reranking/GPU time, database/index memory, object storage, egress, retries and observability. Prices are not hard-coded because models, contracts and pricing change.

## Latency budgets

Treat budgets as provisional targets, not measurements:

| Stage | Initial target to investigate |
|---|---:|
| Auth/cache/history | 20–50 ms |
| Query embedding | 50–250 ms |
| Hybrid search | 30–200 ms |
| Reranking | 20–150 ms |
| Generation | 0.5–4 s, highly model/output dependent |

Actual p95 is not the sum of stage p95s. Measure end-to-end latency and contention under realistic concurrency. This application returns one completed response; time-to-first-token/streaming is not implemented. The local extractive probe is much faster than an external generation workload and must not be extrapolated to it.
