"""Runs with real roles/pgvector in CI. Skipped unless all three URLs are provided."""
import os
import uuid
from dataclasses import replace

import pytest
from sqlalchemy import insert, select, text

from tenantforge.auth import Principal
from tenantforge.config import Settings
from tenantforge.db import Database, conversations, documents
from tenantforge.providers import Providers
from tenantforge.service import Service
from tenantforge.storage import Storage
from tenantforge.worker import Worker

pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    names = ["TEST_ADMIN_DATABASE_URL", "TEST_DATABASE_URL", "TEST_WORKER_DATABASE_URL"]
    if not all(os.getenv(x) for x in names):
        pytest.skip("Real PostgreSQL/pgvector URLs not configured")
    root = tmp_path_factory.mktemp("pg")
    s = Settings(database_url=os.environ[names[0]],storage_dir=str(root/"objects"))
    admin = Database(s)
    with admin.engine.begin() as c:
        for role,password in [("rag_app","local-app-only"),("rag_worker","local-worker-only")]:
            if not c.execute(text("SELECT 1 FROM pg_roles WHERE rolname=:role"),{"role":role}).scalar():
                c.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}' NOSUPERUSER NOBYPASSRLS"))
        c.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
        c.execute(text("GRANT USAGE ON SCHEMA public TO rag_app,rag_worker"))
    admin.initialize()
    app = Database(replace(s,database_url=os.environ[names[1]]))
    workerdb = Database(replace(s,database_url=os.environ[names[2]]))
    p = Principal("pg-"+uuid.uuid4().hex[:12],"alice","editor")
    other = Principal("pg-"+uuid.uuid4().hex[:12],"bob","editor")
    admin.provision(p.tenant)
    admin.provision(other.tenant)
    providers, storage = Providers(s), Storage(s)
    svc = Service(app,storage,providers)
    worker = Worker(workerdb,storage,providers)
    yield svc,worker,p,other,admin
    providers.close()
    for db in (app,workerdb,admin):
        db.engine.dispose()


def test_postgres_real_ingestion_and_hybrid_search(pg):
    svc,worker,p,other,_ = pg
    svc.ingest(p,"policy.txt",b"Annual leave allowance is 25 days.",{"department":"hr"})
    svc.ingest(other,"policy.txt",b"Annual leave allowance is 12 days.",{"department":"hr"})
    for _ in range(10):
        if not worker.once():
            break
    one = svc.query(p,"annual leave allowance",filters={"department":"hr"})
    two = svc.query(other,"annual leave allowance",filters={"department":"hr"})
    assert "25" in one["answer"] and "12" not in one["answer"]
    assert "12" in two["answer"] and "25" not in two["answer"]


def test_rls_without_application_where_clause(pg):
    svc,_,p,other,_ = pg
    with svc.db.tx(p.tenant,p.user) as c:
        assert all(x == p.tenant for x in c.execute(select(documents.c.tenant_id)).scalars())
    with svc.db.tx() as c:
        assert c.execute(select(documents)).first() is None
    with svc.db.tx(other.tenant,other.user) as c:
        assert all(x == other.tenant for x in c.execute(select(documents.c.tenant_id)).scalars())


def test_rls_conversation_user_boundary(pg):
    svc,_,p,_,_ = pg
    svc.query(p,"annual leave")
    with svc.db.tx(p.tenant,"another-user") as c:
        assert c.execute(select(conversations)).first() is None


def test_rls_rejects_cross_tenant_insert(pg):
    svc,_,p,other,_ = pg
    from sqlalchemy.exc import DBAPIError
    with pytest.raises(DBAPIError):
        with svc.db.tx(p.tenant,p.user) as c:
            c.execute(insert(conversations).values(id=str(uuid.uuid4()),tenant_id=other.tenant,user_id=p.user,
                messages=[],version=0,lease_until=0,updated_at=0))


def test_application_role_cannot_bypass_or_disable_rls(pg):
    svc,_,_,_,_ = pg
    from sqlalchemy.exc import DBAPIError
    with svc.db.tx() as c:
        role = c.execute(text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")).one()
        assert not role[0] and not role[1]
    with pytest.raises(DBAPIError):
        with svc.db.tx() as c:
            c.execute(text("ALTER TABLE documents DISABLE ROW LEVEL SECURITY"))
