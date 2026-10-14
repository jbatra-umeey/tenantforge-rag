import time
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import (
    JSON, Column, Float, ForeignKeyConstraint, Index, Integer, MetaData, String, Table,
    Text, UniqueConstraint, create_engine, event, select, text, update,
)
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

meta = MetaData()
jtype = JSON().with_variant(JSONB(), "postgresql")


def tenant_col():
    return Column("tenant_id", String(64), nullable=False, index=True)


tenants = Table("tenants", meta,
    Column("tenant_id", String(64), primary_key=True),
    Column("corpus_version", Integer, nullable=False, default=0),
    Column("embedding_fingerprint", String(300), nullable=False),
)
documents = Table("documents", meta,
    Column("id", String(36), primary_key=True), tenant_col(),
    Column("name", String(240), nullable=False), Column("object_key", String(300), nullable=False),
    Column("fingerprint", String(64), nullable=False), Column("status", String(20), nullable=False),
    Column("metadata", jtype, nullable=False), Column("size", Integer, nullable=False),
    Column("created_at", Float, nullable=False), Column("error", String(200)),
    Column("chunk_count", Integer, nullable=False, default=0),
    UniqueConstraint("tenant_id", "fingerprint"), UniqueConstraint("tenant_id", "id"),
)
chunks = Table("chunks", meta,
    Column("id", String(36), primary_key=True), tenant_col(),
    Column("document_id", String(36), nullable=False), Column("ordinal", Integer, nullable=False),
    Column("content", Text, nullable=False), Column("locator", String(150), nullable=False),
    Column("embedding", jtype, nullable=False),
    ForeignKeyConstraint(["tenant_id", "document_id"], ["documents.tenant_id", "documents.id"], ondelete="CASCADE"),
    UniqueConstraint("tenant_id", "document_id", "ordinal"),
)
jobs = Table("jobs", meta,
    Column("id", String(36), primary_key=True), tenant_col(), Column("document_id", String(36), nullable=False),
    Column("state", String(20), nullable=False), Column("attempts", Integer, nullable=False, default=0),
    Column("available_at", Float, nullable=False), Column("lease_until", Float, nullable=False, default=0),
    Column("fence", String(36)), Column("error", String(200)),
    ForeignKeyConstraint(["tenant_id", "document_id"], ["documents.tenant_id", "documents.id"], ondelete="CASCADE"),
)
Index("ix_jobs_claim", jobs.c.state, jobs.c.available_at, jobs.c.lease_until)
conversations = Table("conversations", meta,
    Column("id", String(36), primary_key=True), tenant_col(), Column("user_id", String(128), nullable=False),
    Column("messages", jtype, nullable=False), Column("version", Integer, nullable=False, default=0),
    Column("lease_until", Float, nullable=False, default=0), Column("fence", String(36)),
    Column("updated_at", Float, nullable=False),
)
cache = Table("query_cache", meta,
    tenant_col(), Column("user_id", String(128), nullable=False),
    Column("key", String(64), primary_key=True), Column("payload", jtype, nullable=False),
    Column("expires_at", Float, nullable=False),
)
embedding_cache = Table("embedding_cache", meta,
    tenant_col(), Column("key", String(64), primary_key=True), Column("vector", jtype, nullable=False),
    Column("created_at", Float, nullable=False),
)
usage = Table("usage", meta,
    Column("tenant_id", String(64), primary_key=True), Column("bucket", String(100), primary_key=True),
    Column("units", Integer, nullable=False),
)
audit = Table("audit_events", meta,
    Column("id", String(36), primary_key=True), tenant_col(), Column("user_id", String(128), nullable=False),
    Column("action", String(40), nullable=False), Column("resource_id", String(64)),
    Column("created_at", Float, nullable=False),
)


class Database:
    def __init__(self, settings):
        self.settings = settings
        self.pg = settings.database_url.startswith("postgresql")
        if not self.pg:
            filename = settings.database_url.removeprefix("sqlite:///")
            if filename != ":memory:":
                Path(filename).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(settings.database_url, pool_pre_ping=True,
            **({"pool_size": 10, "max_overflow": 10} if self.pg else
               {"connect_args": {"check_same_thread": False, "timeout": 30}}))
        if not self.pg:
            @event.listens_for(self.engine, "connect")
            def pragmas(conn, _):
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")

    @contextmanager
    def tx(self, tenant=None, user=None):
        with self.engine.begin() as conn:
            if self.pg:
                conn.execute(text("SELECT set_config('app.tenant_id', :t, true), "
                                  "set_config('app.user_id', :u, true)"), {"t": tenant or "", "u": user or ""})
            yield conn

    def upsert_ignore(self, conn, table, **values):
        insert = pg_insert if self.pg else sqlite_insert
        return conn.execute(insert(table).values(**values).on_conflict_do_nothing())

    def initialize(self):
        """DDL runs explicitly with the migration identity, never during PG API startup."""
        if self.pg:
            with self.engine.begin() as c:
                c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        meta.create_all(self.engine)
        if self.pg:
            with self.engine.begin() as c:
                dim = self.settings.embedding_dimensions
                c.execute(text(f"ALTER TABLE chunks ADD COLUMN IF NOT EXISTS vector vector({dim})"))
                c.execute(text("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS search_vector tsvector "
                    "GENERATED ALWAYS AS (to_tsvector('english', content)) STORED"))
                c.execute(text("CREATE INDEX IF NOT EXISTS chunks_vector_hnsw ON chunks USING hnsw (vector vector_cosine_ops)"))
                c.execute(text("CREATE INDEX IF NOT EXISTS chunks_fts ON chunks USING gin (search_vector)"))
                c.execute(text("CREATE INDEX IF NOT EXISTS documents_metadata ON documents USING gin (metadata)"))
                for table in meta.tables.values():
                    name = table.name  # only static, trusted schema identifiers
                    c.execute(text(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY"))
                    c.execute(text(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY"))
                    condition = "tenant_id = current_setting('app.tenant_id', true)"
                    if table in (conversations, cache):
                        condition += " AND user_id = current_setting('app.user_id', true)"
                    c.execute(text(f"DROP POLICY IF EXISTS tenant_isolation ON {name}"))
                    c.execute(text(f"CREATE POLICY tenant_isolation ON {name} USING ({condition}) WITH CHECK ({condition})"))
                # A narrowly scoped worker policy discovers queue work, not document contents.
                c.execute(text("DROP POLICY IF EXISTS worker_dispatch ON jobs"))
                c.execute(text("CREATE POLICY worker_dispatch ON jobs TO rag_worker USING (true) WITH CHECK (true)"))
                c.execute(text("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO rag_app, rag_worker"))
                c.execute(text("REVOKE ALL ON conversations, query_cache FROM rag_worker"))

    def provision(self, tenant):
        with self.tx(tenant) as c:
            self.upsert_ignore(c, tenants, tenant_id=tenant, corpus_version=0,
                               embedding_fingerprint=self.settings.fingerprint)

    def require_tenant(self, c, tenant):
        row = c.execute(select(tenants).where(tenants.c.tenant_id == tenant)).mappings().first()
        if not row:
            from .errors import Problem
            raise Problem(403, "Tenant is not provisioned")
        if row["embedding_fingerprint"] != self.settings.fingerprint:
            from .errors import Problem
            raise Problem(409, "Embedding configuration changed; use a fresh corpus or reindex")
        return dict(row)

    def bump(self, c, tenant):
        c.execute(update(tenants).where(tenants.c.tenant_id == tenant)
                  .values(corpus_version=tenants.c.corpus_version + 1))

    def reserve(self, c, tenant, bucket, amount, limit):
        from .errors import Problem
        self.upsert_ignore(c, usage, tenant_id=tenant, bucket=bucket, units=0)
        result = c.execute(update(usage).where(usage.c.tenant_id == tenant, usage.c.bucket == bucket,
                         usage.c.units + amount <= limit).values(units=usage.c.units + amount))
        if not result.rowcount:
            raise Problem(429, "Tenant budget or rate limit reached")

    def token_budget(self, c, tenant, units):
        self.reserve(c, tenant, f"tokens:{int(time.time() // 86400)}", units, self.settings.daily_token_budget)

    def ready(self):
        with self.tx() as c:
            c.execute(select(tenants.c.tenant_id).limit(1))
            if self.pg:
                column_type = c.execute(text("SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                    "WHERE attrelid='chunks'::regclass AND attname='vector'")).scalar()
                if column_type != f"vector({self.settings.embedding_dimensions})":
                    raise RuntimeError("Embedding dimensions do not match the database index")
            if self.pg and self.settings.environment == "production":
                role = c.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")).one()
                if role[0] or role[1]:
                    raise RuntimeError("Application role must not bypass RLS")
                insecure = c.execute(text("SELECT count(*) FROM pg_class WHERE relname = ANY(:names) "
                    "AND (NOT relrowsecurity OR NOT relforcerowsecurity OR relowner = "
                    "(SELECT oid FROM pg_roles WHERE rolname=current_user))"), {"names": list(meta.tables)}).scalar()
                if insecure:
                    raise RuntimeError("RLS or application role ownership is unsafe")

