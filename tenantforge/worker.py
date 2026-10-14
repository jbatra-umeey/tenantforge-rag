import json
import logging
import random
import time
import uuid
from pathlib import Path

from sqlalchemy import and_, delete, insert, or_, select, text, update

from .db import chunks, documents, jobs
from .errors import Problem
from .processing import parse_isolated
from .telemetry import JOBS, STAGE, TRACER

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, db, storage, providers):
        self.db, self.storage, self.providers = db, storage, providers
        self.s = db.settings

    def due(self, now):
        return or_(and_(jobs.c.state == "queued", jobs.c.available_at <= now),
                   and_(jobs.c.state == "running", jobs.c.lease_until < now))

    def claim(self):
        now = time.time()
        with self.db.tx() as c:
            stmt = select(jobs).where(self.due(now)).order_by(jobs.c.available_at).limit(1)
            if self.db.pg:
                stmt = stmt.with_for_update(skip_locked=True)
            row = c.execute(stmt).mappings().first()
            if not row:
                return None
            row = dict(row)
            fence = str(uuid.uuid4())
            changed = c.execute(update(jobs).where(jobs.c.id == row["id"], self.due(now),
                jobs.c.attempts == row["attempts"]).values(state="running", attempts=row["attempts"]+1,
                fence=fence, lease_until=now+self.s.lease_seconds))
            if not changed.rowcount:
                return None
            row.update(fence=fence, attempts=row["attempts"]+1)
            return row

    def heartbeat(self, job, connection=None):
        def extend(c):
            result = c.execute(update(jobs).where(jobs.c.id == job["id"], jobs.c.fence == job["fence"],
                jobs.c.state == "running", jobs.c.lease_until >= time.time())
                .values(lease_until=time.time()+self.s.lease_seconds))
            if not result.rowcount:
                raise Problem(409, "Worker lease lost")
        if connection is not None:
            extend(connection)
        else:
            with self.db.tx(job["tenant_id"]) as c:
                extend(c)

    def process(self, job):
        tenant = job["tenant_id"]
        with TRACER.start_as_current_span("ingestion"), STAGE.labels("ingestion").time():
            try:
                if job["attempts"] > self.s.max_attempts:
                    raise ValueError("Attempts exhausted")
                with self.db.tx(tenant) as c:
                    self.db.require_tenant(c, tenant)
                    self.heartbeat(job, c)
                    doc = c.execute(select(documents).where(documents.c.tenant_id == tenant,
                        documents.c.id == job["document_id"], documents.c.status != "deleted")).mappings().first()
                    if not doc:
                        raise ValueError("Document no longer available")
                    c.execute(update(documents).where(documents.c.id == doc["id"], documents.c.tenant_id == tenant,
                              documents.c.status != "deleted")
                              .values(status="processing"))
                self.heartbeat(job)
                parsed = parse_isolated(self.storage.get(tenant, doc["object_key"]), doc["name"],
                                        self.s.max_extracted_chars, self.s.max_chunks)
                vectors = self.providers.cached_embeddings(self.db, tenant, [x.text for x in parsed],
                                                           heartbeat=lambda c=None: self.heartbeat(job, c))
                self.publish(job, parsed, vectors)
                JOBS.labels("ready").inc()
                return True
            except Exception as exc:
                self.fail(job, exc)
                return False

    def publish(self, job, parsed, vectors):
        tenant = job["tenant_id"]
        with self.db.tx(tenant) as c:
            # Lock ordering is jobs -> documents, shared with deletion.
            guard = c.execute(update(jobs).where(jobs.c.id == job["id"], jobs.c.fence == job["fence"],
                jobs.c.state == "running", jobs.c.lease_until >= time.time()).values(state="done", lease_until=0))
            if not guard.rowcount:
                raise Problem(409, "Stale worker cannot publish")
            guard = c.execute(update(documents).where(documents.c.id == job["document_id"],
                documents.c.tenant_id == tenant, documents.c.status != "deleted")
                .values(status="ready", error=None, chunk_count=len(parsed)))
            if not guard.rowcount:
                raise Problem(409, "Deleted document cannot be published")
            c.execute(delete(chunks).where(chunks.c.tenant_id == tenant, chunks.c.document_id == job["document_id"]))
            for i, (block, vector) in enumerate(zip(parsed, vectors, strict=True)):
                chunk_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{tenant}/{job['document_id']}/{i}"))
                c.execute(insert(chunks).values(id=chunk_id, tenant_id=tenant, document_id=job["document_id"],
                    ordinal=i, content=block.text, locator=block.locator, embedding=vector if not self.db.pg else []))
                if self.db.pg:
                    c.execute(text("UPDATE chunks SET vector=CAST(:v AS vector) WHERE id=:id AND tenant_id=:tenant"),
                              {"v": json.dumps(vector), "id": chunk_id, "tenant": tenant})
            self.db.bump(c, tenant)

    def fail(self, job, exc):
        # Never persist parser/provider exceptions containing document contents or secrets.
        permanent = isinstance(exc, ValueError) or job["attempts"] >= self.s.max_attempts
        state = "dead" if permanent else "queued"
        error = "Invalid document or configuration" if isinstance(exc, ValueError) else "Processing failed; inspect worker health"
        with self.db.tx(job["tenant_id"]) as c:
            changed = c.execute(update(jobs).where(jobs.c.id == job["id"], jobs.c.fence == job["fence"],
                jobs.c.state == "running").values(state=state, error=error, lease_until=0,
                    available_at=time.time()+min(60, 2**job["attempts"])+random.uniform(0, 1)))
            if changed.rowcount:
                c.execute(update(documents).where(documents.c.id == job["document_id"],
                    documents.c.tenant_id == job["tenant_id"], documents.c.status != "deleted")
                    .values(status="failed" if permanent else "queued", error=error))
        JOBS.labels(state).inc()
        log.warning(json.dumps({"event": "job_failed", "job_id": job["id"], "type": type(exc).__name__, "state": state}))

    def once(self):
        job = self.claim()
        if job:
            self.process(job)
            return True
        return False

    def run(self, stop=None):
        while stop is None or not stop.is_set():
            Path("/tmp/tenantforge-worker-health").touch()
            try:
                busy = self.once()
            except Exception as exc:
                log.warning(json.dumps({"event": "worker_loop_failed", "type": type(exc).__name__}))
                busy = False
            if not busy:
                if stop:
                    stop.wait(1)
                else:
                    time.sleep(1)

