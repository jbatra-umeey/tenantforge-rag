import time
from dataclasses import replace

import pytest
from sqlalchemy import func, select, update

from tenantforge.db import chunks, conversations, documents, jobs
from tenantforge.errors import Problem

from conftest import auth_header, docx_bytes, pdf_bytes, upload


@pytest.mark.parametrize("name,data", [
    ("policy.txt", b"Employees receive 25 days of annual leave."),
    ("policy.pdf", pdf_bytes()), ("policy.docx", docx_bytes()),
    ("policy.html", b"<h1>Leave</h1><p>Employees receive 25 days of annual leave.</p><script>bad()</script>"),
    ("policy.csv", b"policy,days\nannual leave,25\n"),
], ids=["txt", "pdf", "docx", "html", "csv"])
def test_each_format_upload_to_cited_answer(system, name, data):
    client, svc, worker, auth = system
    doc = upload(system, data, name)
    response = client.post("/v1/query", headers=auth_header(auth), json={"question": "What is the annual leave policy?"})
    assert response.status_code == 200, response.text
    answer = response.json()
    assert "25" in answer["answer"] and answer["citations"]
    assert answer["citations"][0]["document_id"] == doc["document_id"]
    source = client.get(answer["citations"][0]["url"], headers=auth_header(auth)).json()
    assert "25" in source["content"]
    assert client.get("/v1/documents", headers=auth_header(auth)).json()["items"][0]["status"] == "ready"


def test_cross_tenant_documents_retrieval_and_sources(system):
    client, svc, worker, auth = system
    a = upload(system, b"The private launch codename is GOLDEN-TIGER.")
    upload(system, b"The private launch codename is SILVER-MOON.", tenant="beta")
    one = client.post("/v1/query", headers=auth_header(auth), json={"question": "What is the launch codename?"}).json()
    two = client.post("/v1/query", headers=auth_header(auth, "beta"), json={"question": "What is the launch codename?"}).json()
    assert "GOLDEN-TIGER" in one["answer"] and "SILVER" not in one["answer"]
    assert "SILVER-MOON" in two["answer"] and "GOLDEN" not in two["answer"]
    assert client.get(one["citations"][0]["url"], headers=auth_header(auth, "beta")).status_code == 404
    assert client.delete(f"/v1/documents/{a['document_id']}", headers=auth_header(auth, "beta")).status_code == 404
    assert len(client.get("/v1/documents", headers=auth_header(auth, "beta")).json()["items"]) == 1


@pytest.mark.parametrize("tenant,user", [("acme", "bob"), ("beta", "alice")])
def test_conversation_owner_isolation(system, tenant, user):
    client, _, _, auth = system
    upload(system)
    answer = client.post("/v1/query", headers=auth_header(auth), json={"question": "annual leave"}).json()
    cid = answer["conversation_id"]
    other = auth_header(auth, tenant, user)
    assert client.get(f"/v1/conversations/{cid}", headers=other).status_code == 404
    assert client.delete(f"/v1/conversations/{cid}", headers=other).status_code == 404
    assert client.post("/v1/query", headers=other, json={"question":"leave", "conversation_id":cid}).status_code == 404


def test_reader_can_query_cannot_mutate(system):
    client, _, _, auth = system
    doc = upload(system)
    headers = auth_header(auth, role="reader")
    assert client.post("/v1/query", headers=headers, json={"question":"annual leave"}).status_code == 200
    assert client.post("/v1/documents", headers=headers, files={"file":("a.txt", b"hello")}).status_code == 403
    assert client.delete(f"/v1/documents/{doc['document_id']}", headers=headers).status_code == 403


def test_no_auth_and_forged_auth(system):
    client, _, _, auth = system
    assert client.get("/v1/documents").status_code == 401
    token = auth.issue("acme")
    assert client.get("/v1/documents", headers={"Authorization":"Bearer " + token[:-10] + "0000000000"}).status_code == 401
    assert client.get("/v1/documents", headers={"Authorization":"Bearer " + auth.issue("acme", seconds=-1)}).status_code == 401


def test_body_tenant_override_rejected_and_header_ignored(system):
    client, _, _, auth = system
    upload(system)
    headers = {**auth_header(auth), "X-Tenant-ID":"beta"}
    assert len(client.get("/v1/documents", headers=headers).json()["items"]) == 1
    assert client.post("/v1/query", headers=headers, json={"question":"hello", "tenant_id":"beta"}).status_code == 422


def test_unknown_tenant_denied(system):
    client, _, _, auth = system
    assert client.get("/v1/documents", headers=auth_header(auth,"unknown")).status_code == 403


def test_metadata_filter_before_ranking(system):
    client, _, _, auth = system
    upload(system, b"Sales annual leave is 12 days.", "sales.txt", metadata={"department":"sales"})
    upload(system, b"Engineering annual leave is 25 days.", "eng.txt", metadata={"department":"engineering"})
    answer = client.post("/v1/query", headers=auth_header(auth),
        json={"question":"annual leave", "filters":{"department":"engineering"}}).json()
    assert "25" in answer["answer"] and "12" not in answer["answer"]
    none = client.post("/v1/query", headers=auth_header(auth),
        json={"question":"annual leave", "filters":{"department":"missing"}}).json()
    assert none["abstained"] and not none["citations"]


def test_cache_isolation_and_reuse(system):
    client, _, _, auth = system
    upload(system)
    def query(tenant="acme", user="alice"):
        return client.post("/v1/query", headers=auth_header(auth,tenant,user), json={"question":"annual leave"}).json()
    assert not query()["cache_hit"]
    assert query()["cache_hit"]
    assert not query(user="bob")["cache_hit"]
    assert not query(tenant="beta")["cache_hit"]


def test_delete_invalidates_sources_cache_and_prompt_memory(system, monkeypatch, alice):
    client, svc, _, auth = system
    doc = upload(system)
    first = svc.query(alice, "What is the annual leave policy?")
    svc.remove_document(alice, doc["document_id"])
    second = svc.query(alice, "What is the annual leave policy?")
    assert second["abstained"] and not second["cache_hit"]
    assert client.get(first["citations"][0]["url"], headers=auth_header(auth)).status_code == 404
    seen = []
    original = svc.providers.generate
    def generate(q, sources, history, quality):
        seen.extend(history)
        return original(q, sources, history, quality)
    monkeypatch.setattr(svc.providers, "generate", generate)
    svc.query(alice, "When does it begin?", first["conversation_id"])
    assert not seen


def test_mid_query_corpus_change_is_rejected(system, monkeypatch, alice):
    _, svc, _, _ = system
    doc = upload(system)
    original = svc.providers.generate
    def changed(*args):
        answer = original(*args)
        svc.remove_document(alice, doc["document_id"])
        return answer
    monkeypatch.setattr(svc.providers, "generate", changed)
    with pytest.raises(Problem, match="Corpus changed"):
        svc.query(alice, "annual leave")


def test_deduplication_is_tenant_local(system):
    client, svc, _, auth = system
    first = upload(system)
    again = upload(system)
    other = upload(system, tenant="beta")
    assert again["deduplicated"] and again["document_id"] == first["document_id"]
    assert not other["deduplicated"] and other["document_id"] != first["document_id"]
    assert len(list(svc.storage.root.glob("acme/*"))) == 1


def test_reupload_after_deletion(system, alice):
    _, svc, _, _ = system
    first = upload(system)
    svc.remove_document(alice, first["document_id"])
    second = upload(system)
    assert not second["deduplicated"] and second["document_id"] != first["document_id"]
    assert svc.query(alice,"annual leave")["citations"]


def test_queue_is_asynchronous_and_survives_new_worker(system):
    client, svc, worker, auth = system
    from tenantforge.worker import Worker
    response = client.post("/v1/documents", headers=auth_header(auth), files={"file":("a.txt",b"Durable queues retain work.")})
    assert response.json()["status"] == "queued"
    assert not client.post("/v1/query", headers=auth_header(auth), json={"question":"durable queues"}).json()["citations"]
    assert Worker(svc.db, svc.storage, svc.providers).once()
    assert client.post("/v1/query", headers=auth_header(auth), json={"question":"durable queues"}).json()["citations"]


def test_bad_document_dead_letter_and_manual_retry(system):
    client, svc, worker, auth = system
    doc = upload(system, b"not a pdf", "broken.pdf")
    listing = client.get("/v1/documents", headers=auth_header(auth)).json()["items"]
    assert listing[0]["status"] == "failed"
    assert client.post(f"/v1/documents/{doc['document_id']}/retry", headers=auth_header(auth)).status_code == 200
    assert worker.once()
    with svc.db.tx("acme") as c:
        assert c.execute(select(jobs.c.state)).scalar() == "dead"


def test_stale_worker_cannot_publish_after_reclaim(system):
    client, svc, worker, auth = system
    client.post("/v1/documents", headers=auth_header(auth), files={"file":("a.txt",b"hello workers")})
    first = worker.claim()
    with svc.db.tx("acme") as c:
        c.execute(update(jobs).where(jobs.c.id == first["id"]).values(lease_until=0))
    second = worker.claim()
    assert first["fence"] != second["fence"]
    with pytest.raises(Problem, match="Stale worker"):
        worker.publish(first, [], [])
    assert worker.process(second)


def test_delete_fences_active_worker(system, alice):
    client, svc, worker, auth = system
    response = client.post("/v1/documents", headers=auth_header(auth), files={"file":("a.txt",b"hello workers")}).json()
    job = worker.claim()
    svc.remove_document(alice, response["document_id"])
    assert not worker.process(job)
    with svc.db.tx("acme") as c:
        assert c.execute(select(documents.c.status)).scalar() == "deleted"
        assert c.execute(select(func.count()).select_from(chunks)).scalar() == 0


def test_retryable_provider_failure_and_attempt_limit(system, monkeypatch):
    client, svc, worker, auth = system
    client.post("/v1/documents", headers=auth_header(auth), files={"file":("a.txt",b"hello workers")})
    def fail(*args, **kwargs):
        raise Problem(502, "Provider unavailable")
    monkeypatch.setattr(svc.providers, "cached_embeddings", fail)
    for attempt in range(svc.s.max_attempts):
        assert worker.once()
        with svc.db.tx("acme") as c:
            row = c.execute(select(jobs)).mappings().first()
            assert row["attempts"] == attempt+1
            assert row["state"] == ("dead" if attempt == svc.s.max_attempts-1 else "queued")
            c.execute(update(jobs).values(available_at=0))


def test_conversation_serialization_and_followup_memory(system, alice):
    _, svc, _, _ = system
    upload(system)
    first = svc.query(alice, "What is the annual leave policy?")
    second = svc.query(alice, "When does it begin?", first["conversation_id"])
    assert second["conversation_id"] == first["conversation_id"]
    assert "January" in second["answer"]
    assert len(svc.conversation(alice, first["conversation_id"])["messages"]) == 4
    with svc.db.tx("acme", "alice") as c:
        c.execute(update(conversations).where(conversations.c.id == first["conversation_id"]).values(lease_until=time.time()+300))
    with pytest.raises(Problem, match="Another query"):
        svc.query(alice,"leave",first["conversation_id"])


def test_backpressure_and_rate_budget_are_enforced(system, alice):
    _, svc, _, _ = system
    svc.s = replace(svc.s, max_pending_jobs=1)
    svc.ingest(alice,"one.txt",b"one",{})
    with pytest.raises(Problem, match="capacity"):
        svc.ingest(alice,"two.txt",b"two",{})
    with svc.db.tx("acme") as c:
        svc.db.reserve(c,"acme","test",2,2)
    with pytest.raises(Problem):
        with svc.db.tx("acme") as c:
            svc.db.reserve(c,"acme","test",1,2)


def test_token_budget_fails_closed(system, alice):
    _, svc, _, _ = system
    with svc.db.tx("acme") as c:
        svc.db.token_budget(c, "acme", svc.s.daily_token_budget)
    with pytest.raises(Problem, match="budget"):
        svc.query(alice,"annual leave")


def test_embedding_mismatch_fails_closed(system, alice):
    _, svc, _, _ = system
    from tenantforge.db import tenants
    with svc.db.tx("acme") as c:
        c.execute(update(tenants).where(tenants.c.tenant_id == "acme").values(embedding_fingerprint="different"))
    with pytest.raises(Problem, match="Embedding configuration"):
        svc.query(alice,"hello")


@pytest.mark.parametrize("body", [{"question":" "}, {"question":"hello","top_k":99},
    {"question":"hello","filters":{"a":4}}, {"question":"hello","quality":"unknown"}])
def test_input_validation(system, body):
    client, _, _, auth = system
    assert client.post("/v1/query", headers=auth_header(auth), json=body).status_code == 422


def test_unsupported_extension_and_metadata_rejected(system):
    client, _, _, auth = system
    headers = auth_header(auth)
    assert client.post("/v1/documents", headers=headers, files={"file":("a.exe",b"x")}).status_code == 415
    assert client.post("/v1/documents", headers=headers, files={"file":("a.txt",b"x")},
                       data={"metadata":"[1,2]"}).status_code == 422


def test_health_static_metrics_and_security_headers(system):
    client, _, _, _ = system
    assert client.get("/readyz").status_code == 200
    assert "TenantForge" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
    assert "tenantforge_http_requests" in client.get("/metrics").text
    assert client.get("/").headers["X-Content-Type-Options"] == "nosniff"
