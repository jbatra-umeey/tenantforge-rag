import io
import json
import math
import zipfile
from dataclasses import replace

import httpx
import pytest

from tenantforge.auth import Auth
from tenantforge.config import Settings
from tenantforge.errors import Problem
from tenantforge.processing import Block, chunk, parse, parse_isolated
from tenantforge.providers import Providers, normalize

from conftest import auth_header, pdf_bytes


def test_sentence_boundaries_overlap_and_hard_limit():
    text = "First sentence describes isolation. " * 150
    result = chunk([Block(text,"page 1")])
    assert len(result) > 1
    assert all(len(x.text) <= 1200 and x.locator == "page 1" for x in result)
    assert all(len(x.text) <= 1200 for x in chunk([Block("X" * 6000,"long")]))


def test_html_removes_executable_content_and_preserves_locators():
    result = parse(b"<h1>Policy</h1><p>Annual leave is 25 days.</p><script>SECRET_SCRIPT</script>", "a.html")
    assert "SECRET_SCRIPT" not in str(result)
    assert result[-1].locator == "Policy"


def test_docx_expansion_limit():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", b"x" * 30_000_001)
    with pytest.raises(ValueError, match="expansion"):
        parse(out.getvalue(), "bomb.docx")


@pytest.mark.parametrize("data,name", [(b"", "a.txt"), (b"\xff\xff", "a.txt"), (b"not pdf", "a.pdf")])
def test_invalid_input_rejected(data,name):
    with pytest.raises((ValueError, UnicodeDecodeError)):
        parse(data,name)


def test_extracted_text_and_chunk_limits():
    with pytest.raises(ValueError, match="text exceeds"):
        parse(b"too much data", "x.txt", max_chars=3)
    with pytest.raises(ValueError, match="Chunk limit"):
        chunk([Block("x"*5000,"section")], max_chunks=1)


def test_real_sandboxed_pdf_parser():
    result = parse_isolated(pdf_bytes(), "a.pdf", 10000, 20)
    assert "25 days" in result[0].text


def test_real_sandboxed_parser_failure():
    with pytest.raises(ValueError, match="parsing failed"):
        parse_isolated(b"broken", "a.pdf", 10000, 20)


def test_openai_embedding_contract_and_order(settings):
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert request.url.path == "/v1/embeddings"
        return httpx.Response(200, json={"data":[{"index":1,"embedding":[0,2,0]}, {"index":0,"embedding":[1,0,0]}]})
    provider = Providers(replace(settings, embedding_backend="openai", embedding_model="test", embedding_dimensions=3,
                                 provider_api_key="test"), httpx.MockTransport(handler))
    assert provider.embed(["alpha", "beta"]) == [[1,0,0],[0,1,0]]
    assert calls[0]["dimensions"] == 3
    provider.close()


@pytest.mark.parametrize("vector", [[0,0,0],[math.nan,1,0],[1,0]])
def test_invalid_embedding_rejected(vector):
    with pytest.raises(ValueError):
        normalize(vector,3)


@pytest.mark.parametrize("text,valid", [("Leave is 25 days [1].", True), ("It is 100 [99].", False), ("No citation", False)])
def test_generation_citations_checked_and_route_selected(settings,text,valid):
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200,json={"choices":[{"message":{"content":text}}],"usage":{"total_tokens":30}})
    provider = Providers(replace(settings,generation_backend="openai",llm_model="small",llm_complex_model="large",
                                 provider_api_key="test"), httpx.MockTransport(handler))
    result = provider.generate("leave?",[{"content":"25 days"}],[],"deep")
    assert result["abstained"] is not valid
    assert calls[0]["model"] == "large" and "untrusted" in calls[0]["messages"][0]["content"]
    provider.close()


def test_provider_retries_transient_error(settings):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(200,json={"ok":True})
    provider = Providers(settings,httpx.MockTransport(handler))
    assert provider.post("test",{}) == {"ok":True}
    assert len(calls) == 2
    provider.close()


def test_provider_failure_is_sanitized(settings):
    provider = Providers(settings,httpx.MockTransport(lambda _: httpx.Response(401,text="SECRET_ERROR")))
    with pytest.raises(Problem) as exc:
        provider.post("test",{})
    assert "SECRET" not in str(exc.value)
    provider.close()


def test_embedding_batching_cache_and_tenant_boundary(system,monkeypatch):
    _,svc,_,_ = system
    svc.providers.s = replace(svc.s,embedding_batch_size=2)
    calls = []
    original = svc.providers.embed
    def counted(texts):
        calls.append(texts)
        return original(texts)
    monkeypatch.setattr(svc.providers,"embed",counted)
    texts = ["alpha","beta","gamma","delta","epsilon"]
    svc.providers.cached_embeddings(svc.db,"acme",texts)
    assert list(map(len,calls)) == [2,2,1]
    svc.providers.cached_embeddings(svc.db,"acme",texts)
    assert len(calls) == 3
    svc.providers.cached_embeddings(svc.db,"beta",texts)
    assert len(calls) == 6


def test_storage_rejects_traversal_and_tenant_mismatch(system):
    _,svc,_,_ = system
    key = svc.storage.put("acme",b"secret")
    with pytest.raises(ValueError):
        svc.storage.get("beta",key)
    with pytest.raises(ValueError):
        svc.storage.get("acme","acme/../../escape")


def test_oidc_signature_and_claims(settings,monkeypatch):
    import time
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537,key_size=2048)
    auth = Auth(replace(settings,auth_mode="oidc",jwks_url="https://id.example/jwks"))
    class SigningKey:
        key = None
    signing = SigningKey()
    signing.key = key.public_key()
    monkeypatch.setattr(auth.jwks,"get_signing_key_from_jwt",lambda token:signing)
    payload = {"sub":"alice","tenant_id":"acme","role":"editor","iat":int(time.time()),
               "exp":int(time.time())+300,"iss":settings.jwt_issuer,"aud":settings.jwt_audience}
    token = jwt.encode(payload,key,algorithm="RS256")
    assert auth.verify("Bearer "+token).tenant == "acme"
    payload["aud"] = "wrong"
    with pytest.raises(Problem):
        auth.verify("Bearer "+jwt.encode(payload,key,algorithm="RS256"))
    with pytest.raises(ValueError):
        auth.issue("acme")


def test_production_rejects_demo_configuration():
    with pytest.raises(ValueError,match="PostgreSQL"):
        Settings(environment="production").validate()


def test_oversized_upload_and_declared_body(system):
    client,svc,_,auth = system
    svc.s = replace(svc.s,max_upload_bytes=10)
    assert client.post("/v1/documents",headers=auth_header(auth),files={"file":("a.txt",b"a"*11)}).status_code == 413
    assert client.post("/v1/query",headers={**auth_header(auth),"Content-Length":"999999999"},content=b"{}").status_code == 413


def test_budget_reservation_is_atomic(system):
    from concurrent.futures import ThreadPoolExecutor
    _,svc,_,_ = system
    def reserve(_):
        try:
            with svc.db.tx("acme") as c:
                svc.db.reserve(c,"acme","race",1,5)
            return True
        except Problem:
            return False
    with ThreadPoolExecutor(max_workers=10) as executor:
        assert sum(executor.map(reserve,range(20))) == 5
