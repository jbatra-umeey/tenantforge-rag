import hashlib
import json
import re
import time
import uuid
from pathlib import Path

from sqlalchemy import delete, func, insert, select, update

from .db import audit, cache, chunks, conversations, documents, embedding_cache, jobs, tenants, usage
from .errors import Problem
from .processing import ALLOWED
from .retrieval import hybrid
from .telemetry import CACHE, STAGE, TRACER


def uid():
    return str(uuid.uuid4())


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Service:
    def __init__(self, db, storage, providers):
        self.db, self.storage, self.providers = db, storage, providers
        self.s = db.settings

    def event(self, c, p, action, resource=None):
        c.execute(insert(audit).values(id=uid(), tenant_id=p.tenant, user_id=p.user, action=action,
                                     resource_id=resource, created_at=time.time()))

    def ingest(self, p, name, data, metadata):
        p.write()
        name = Path(name.replace("\\", "/")).name[:240]
        if Path(name).suffix.lower() not in ALLOWED:
            raise Problem(415, "Supported: PDF, DOCX, HTML, CSV, TXT and Markdown")
        if not data or len(data) > self.s.max_upload_bytes:
            raise Problem(413, "Empty file or upload too large")
        fingerprint = digest([hashlib.sha256(data).hexdigest(), name, metadata])
        key = self.storage.put(p.tenant, data)
        used = False
        try:
            with self.db.tx(p.tenant, p.user) as c:
                self.db.require_tenant(c, p.tenant)
                # Short per-tenant lock makes concurrent admission checks atomic.
                c.execute(update(tenants).where(tenants.c.tenant_id == p.tenant)
                          .values(corpus_version=tenants.c.corpus_version))
                existing = c.execute(select(documents).where(documents.c.tenant_id == p.tenant,
                    documents.c.fingerprint == fingerprint)).mappings().first()
                if existing and existing["status"] != "deleted":
                    return {"document_id": existing["id"], "status": existing["status"], "deduplicated": True}
                pending = c.execute(select(func.count()).select_from(jobs).where(jobs.c.tenant_id == p.tenant,
                    jobs.c.state.in_(["queued", "running"]))).scalar()
                count = c.execute(select(func.count()).select_from(documents).where(documents.c.tenant_id == p.tenant,
                    documents.c.status != "deleted")).scalar()
                if pending >= self.s.max_pending_jobs or count >= self.s.max_documents:
                    raise Problem(429, "Tenant ingestion capacity reached")
                if existing:
                    c.execute(delete(documents).where(documents.c.tenant_id == p.tenant, documents.c.id == existing["id"]))
                document_id, job_id = uid(), uid()
                c.execute(insert(documents).values(id=document_id, tenant_id=p.tenant, name=name, object_key=key,
                    fingerprint=fingerprint, status="queued", metadata=metadata, size=len(data),
                    created_at=time.time(), chunk_count=0))
                c.execute(insert(jobs).values(id=job_id, tenant_id=p.tenant, document_id=document_id,
                    state="queued", attempts=0, available_at=time.time(), lease_until=0))
                self.event(c, p, "document.upload", document_id)
            used = True
            return {"document_id": document_id, "job_id": job_id, "status": "queued", "deduplicated": False}
        finally:
            if not used:
                self.storage.delete(p.tenant, key)

    def list_documents(self, p, offset=0, limit=50):
        with self.db.tx(p.tenant, p.user) as c:
            self.db.require_tenant(c, p.tenant)
            cols = [documents.c[x] for x in ("id", "name", "status", "metadata", "size", "created_at", "chunk_count", "error")]
            return [dict(x) for x in c.execute(select(*cols).where(documents.c.tenant_id == p.tenant,
                documents.c.status != "deleted").order_by(documents.c.created_at.desc()).offset(offset).limit(limit)).mappings()]

    def remove_document(self, p, document_id):
        p.write()
        with self.db.tx(p.tenant, p.user) as c:
            doc = c.execute(select(documents).where(documents.c.tenant_id == p.tenant,
                documents.c.id == document_id)).mappings().first()
            if not doc:
                raise Problem(404, "Document not found")
            c.execute(update(jobs).where(jobs.c.tenant_id == p.tenant, jobs.c.document_id == document_id)
                      .values(state="cancelled", fence=uid(), lease_until=0))
            c.execute(update(documents).where(documents.c.tenant_id == p.tenant, documents.c.id == document_id)
                      .values(status="deleted", chunk_count=0))
            c.execute(delete(chunks).where(chunks.c.tenant_id == p.tenant, chunks.c.document_id == document_id))
            c.execute(delete(embedding_cache).where(embedding_cache.c.tenant_id == p.tenant))
            # Corpus-version cache keys invalidate all users without bypassing user RLS.
            c.execute(delete(cache).where(cache.c.tenant_id == p.tenant, cache.c.user_id == p.user))
            self.db.bump(c, p.tenant)
            self.event(c, p, "document.delete", document_id)
        try:
            self.storage.delete(p.tenant, doc["object_key"])
        except Exception:
            # The tombstone keeps the object key for a later purge retry.
            return {"deleted": True, "object_cleanup_pending": True}
        return {"deleted": True, "object_cleanup_pending": False}

    def retry(self, p, document_id):
        p.write()
        with self.db.tx(p.tenant, p.user) as c:
            job = c.execute(select(jobs).where(jobs.c.tenant_id == p.tenant, jobs.c.document_id == document_id,
                jobs.c.state == "dead")).mappings().first()
            if not job:
                raise Problem(404, "Failed job not found")
            c.execute(update(jobs).where(jobs.c.id == job["id"], jobs.c.tenant_id == p.tenant)
                      .values(state="queued", attempts=0, available_at=time.time(), error=None))
            c.execute(update(documents).where(documents.c.id == document_id, documents.c.tenant_id == p.tenant,
                documents.c.status == "failed").values(status="queued", error=None))
            self.event(c, p, "document.retry", document_id)
        return {"status": "queued"}

    def get_chunk(self, p, chunk_id):
        with self.db.tx(p.tenant, p.user) as c:
            row = c.execute(select(chunks.c.id, chunks.c.content, chunks.c.locator, documents.c.name)
                .join(documents, (chunks.c.document_id == documents.c.id) & (chunks.c.tenant_id == documents.c.tenant_id))
                .where(chunks.c.tenant_id == p.tenant, chunks.c.id == chunk_id, documents.c.status == "ready")).mappings().first()
            if not row:
                raise Problem(404, "Source not found")
            return dict(row)

    def conversation(self, p, conversation_id, remove=False):
        with self.db.tx(p.tenant, p.user) as c:
            clause = (conversations.c.id == conversation_id) & (conversations.c.tenant_id == p.tenant) & (conversations.c.user_id == p.user)
            row = c.execute(select(conversations).where(clause)).mappings().first()
            if not row:
                raise Problem(404, "Conversation not found")
            if remove:
                c.execute(delete(conversations).where(clause))
                self.event(c, p, "conversation.delete", conversation_id)
                return {"deleted": True}
            return {"id": row["id"], "messages": row["messages"], "version": row["version"]}

    def acquire_conversation(self, p, conversation_id):
        fence = uid()
        with self.db.tx(p.tenant, p.user) as c:
            tenant = self.db.require_tenant(c, p.tenant)
            self.db.reserve(c, p.tenant, f"queries:{int(time.time()//60)}", 1, self.s.query_per_minute)
            if conversation_id is None:
                conversation_id = uid()
                c.execute(insert(conversations).values(id=conversation_id, tenant_id=p.tenant, user_id=p.user,
                    messages=[], version=0, lease_until=0, updated_at=time.time()))
            clause = (conversations.c.id == conversation_id) & (conversations.c.tenant_id == p.tenant) & (conversations.c.user_id == p.user)
            row = c.execute(select(conversations).where(clause)).mappings().first()
            if not row:
                raise Problem(404, "Conversation not found")
            result = c.execute(update(conversations).where(clause, conversations.c.lease_until < time.time())
                .values(fence=fence, lease_until=time.time()+300))
            if not result.rowcount:
                raise Problem(409, "Another query is active in this conversation")
            return {**dict(row), "fence": fence}, tenant["corpus_version"]

    def query(self, p, question, conversation_id=None, filters=None, top_k=5, quality="standard"):
        conv, revision = self.acquire_conversation(p, conversation_id)
        try:
            return self._query(p, question, conv, revision, filters or {}, top_k, quality)
        finally:
            with self.db.tx(p.tenant, p.user) as c:
                c.execute(update(conversations).where(conversations.c.id == conv["id"],
                    conversations.c.tenant_id == p.tenant, conversations.c.user_id == p.user,
                    conversations.c.fence == conv["fence"]).values(lease_until=0))

    def _query(self, p, question, conv, revision, filters, top_k, quality):
        started = time.perf_counter()
        history = []
        chars = 0
        for message in reversed(conv["messages"]):
            if message.get("corpus_version") != revision:
                continue
            if chars + len(message["content"]) > self.s.max_history_chars:
                break
            history.insert(0, message)
            chars += len(message["content"])
        retrieval_query = question
        if re.search(r"\b(it|its|they|their|that|those|this)\b", question.lower()) or len(question.split()) < 5:
            previous = next((x["content"] for x in reversed(history) if x["role"] == "user"), "")
            retrieval_query = (previous + " " + question).strip()[-3000:]
        key = digest([p.tenant, p.user, revision, question, history, filters, top_k, quality,
            self.s.fingerprint, self.s.generation_backend, self.s.llm_model, self.s.llm_complex_model,
            self.s.reranker, self.s.reranker_model, self.s.max_context_chars, self.s.max_output_tokens, "prompt-v1"])
        with self.db.tx(p.tenant, p.user) as c:
            cached = c.execute(select(cache.c.payload).where(cache.c.key == key, cache.c.tenant_id == p.tenant,
                cache.c.user_id == p.user, cache.c.expires_at > time.time())).scalar()
        if cached:
            answer = dict(cached)
            CACHE.labels("hit").inc()
        else:
            CACHE.labels("miss").inc()
            with TRACER.start_as_current_span("retrieval"), STAGE.labels("retrieval").time():
                vector = self.providers.cached_embeddings(self.db, p.tenant, [retrieval_query])[0]
                candidates = hybrid(self.db, p, retrieval_query, vector, filters)
                ranked = self.providers.rerank(retrieval_query, candidates)
                sources, size = [], 0
                for row in ranked:
                    if len(sources) >= top_k:
                        break
                    if size + len(row["content"]) > self.s.max_context_chars:
                        continue
                    sources.append(row)
                    size += len(row["content"])
            with self.db.tx(p.tenant, p.user) as c:
                self.db.token_budget(c, p.tenant, len((retrieval_query + "".join(x["content"] for x in sources)
                    + "".join(x["content"] for x in history)).encode()) + self.s.max_output_tokens + 1500)
            with TRACER.start_as_current_span("generation"), STAGE.labels("generation").time():
                result = self.providers.generate(question, sources, history, quality)
            citations = [{"number": i, "chunk_id": sources[i-1]["id"], "document_id": sources[i-1]["document_id"],
                "name": sources[i-1]["name"], "locator": sources[i-1]["locator"], "excerpt": sources[i-1]["content"][:500],
                "url": f"/v1/chunks/{sources[i-1]['id']}"} for i in sorted(set(result.pop("cited")))]
            answer = {**result, "citations": citations, "retrieval_mode": self.s.embedding_backend,
                      "reranker": self.s.reranker, "corpus_version": revision}
        with self.db.tx(p.tenant, p.user) as c:
            # A no-op update locks the corpus revision against publication/deletion until commit.
            unchanged = c.execute(update(tenants).where(tenants.c.tenant_id == p.tenant,
                tenants.c.corpus_version == revision).values(corpus_version=revision))
            if not unchanged.rowcount:
                raise Problem(409, "Corpus changed during query; retry for fresh sources")
            messages = conv["messages"] + [{"role": "user", "content": question, "corpus_version": revision},
                {"role": "assistant", "content": answer["answer"], "corpus_version": revision}]
            result = c.execute(update(conversations).where(conversations.c.id == conv["id"],
                conversations.c.tenant_id == p.tenant, conversations.c.user_id == p.user,
                conversations.c.version == conv["version"], conversations.c.fence == conv["fence"],
                conversations.c.lease_until >= time.time()).values(messages=messages[-20:],
                    version=conv["version"]+1, updated_at=time.time()))
            if not result.rowcount:
                raise Problem(409, "Conversation changed during query; retry")
            if not cached:
                c.execute(delete(cache).where(cache.c.key == key, cache.c.tenant_id == p.tenant, cache.c.user_id == p.user))
                c.execute(insert(cache).values(tenant_id=p.tenant, user_id=p.user, key=key, payload=answer,
                    expires_at=time.time()+self.s.cache_ttl_seconds))
            self.event(c, p, "query.complete", conv["id"])
        return {**answer, "conversation_id": conv["id"], "cache_hit": bool(cached),
                "latency_ms": round((time.perf_counter()-started)*1000, 2)}

    def stats(self, p):
        with self.db.tx(p.tenant, p.user) as c:
            self.db.require_tenant(c, p.tenant)
            counts = dict(c.execute(select(documents.c.status, func.count()).where(documents.c.tenant_id == p.tenant)
                                   .group_by(documents.c.status)).all())
            units = c.execute(select(usage.c.units).where(usage.c.tenant_id == p.tenant,
                usage.c.bucket == f"tokens:{int(time.time()//86400)}")).scalar() or 0
            return {"tenant": p.tenant, "user": p.user, "role": p.role, "documents": counts,
                "daily_reserved_token_units": units, "daily_token_budget": self.s.daily_token_budget,
                "embedding_backend": self.s.embedding_backend, "generation_backend": self.s.generation_backend}
