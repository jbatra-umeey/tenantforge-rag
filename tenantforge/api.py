import json
import logging
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .auth import Auth
from .config import Settings
from .db import Database
from .errors import Problem
from .providers import Providers
from .service import Service
from .storage import Storage
from .telemetry import LATENCY, REQUESTS, TRACER, configure


class QueryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: uuid.UUID | None = None
    filters: dict[str, str] = Field(default_factory=dict)
    top_k: int = Field(default=5, ge=1, le=10)
    quality: str = "standard"

    @field_validator("question")
    @classmethod
    def question_not_blank(cls, v):
        if not v.strip():
            raise ValueError("Question cannot be blank")
        return v.strip()

    @field_validator("filters")
    @classmethod
    def bounded_metadata(cls, v):
        if len(v) > 20 or any(not k or len(k) > 64 or len(value) > 256 for k, value in v.items()):
            raise ValueError("Metadata exceeds limits")
        return v

    @field_validator("quality")
    @classmethod
    def supported_quality(cls, v):
        if v not in {"standard", "deep"}:
            raise ValueError("Quality must be standard or deep")
        return v


class RequestTooLarge(Exception):
    pass


class BodyLimit:
    def __init__(self, app, limit):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return await JSONResponse({"detail": "Invalid content length"}, 400)(scope, receive, send)
        if declared > self.limit:
            return await JSONResponse({"detail": "Request too large"}, 413)(scope, receive, send)
        size = 0

        async def limited_receive():
            nonlocal size
            message = await receive()
            size += len(message.get("body", b""))
            if size > self.limit:
                raise RequestTooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except RequestTooLarge:
            await JSONResponse({"detail": "Request too large"}, 413)(scope, receive, send)


def create_app(settings=None):
    s = settings or Settings.from_env()
    s.validate()
    configure(s)
    db, storage, providers, auth = Database(s), Storage(s), Providers(s), Auth(s)
    service = Service(db, storage, providers)

    @asynccontextmanager
    async def lifespan(app):
        if not db.pg:
            db.initialize()
        db.ready()
        yield
        providers.close()
        db.engine.dispose()

    app = FastAPI(title="TenantForge RAG", version="0.1.0", lifespan=lifespan)
    app.state.service, app.state.auth = service, auth
    app.add_middleware(BodyLimit, limit=s.max_upload_bytes + 65536)

    @app.exception_handler(Problem)
    async def problem_handler(request, exc):
        return JSONResponse({"detail": exc.detail}, exc.status,
            headers={"Retry-After": "60"} if exc.status == 429 else None)

    @app.middleware("http")
    async def telemetry(request: Request, call_next):
        start, request_id = time.perf_counter(), str(uuid.uuid4())
        with TRACER.start_as_current_span("http.request"):
            response = await call_next(request)
        route = getattr(request.scope.get("route"), "path", "unmatched")
        LATENCY.labels(route).observe(time.perf_counter()-start)
        REQUESTS.labels(route, str(response.status_code)).inc()
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'"
        if request.url.path in {"/docs", "/redoc"}:
            response.headers["Content-Security-Policy"] = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https://fastapi.tiangolo.com; frame-ancestors 'none'")
        response.headers["Cache-Control"] = "no-store"
        logging.getLogger(__name__).info(json.dumps({"request_id": request_id, "route": route,
            "status": response.status_code, "ms": round((time.perf_counter()-start)*1000)}))
        return response

    def principal(authorization: str | None = Header(default=None)):
        return auth.verify(authorization)

    @app.get("/healthz")
    def health():
        return {"status": "alive"}

    @app.get("/readyz")
    def ready():
        try:
            db.ready()
            return {"status": "ready"}
        except Exception:
            return JSONResponse({"status": "not_ready"}, 503)

    @app.get("/metrics")
    def metrics(authorization: str | None = Header(default=None)):
        if s.metrics_token and not secrets.compare_digest(authorization or "", f"Bearer {s.metrics_token}"):
            raise Problem(401, "Metrics token required")
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/v1/documents", status_code=202)
    def upload(file: UploadFile = File(...), metadata: str = Form("{}"), p=Depends(principal)):
        try:
            values = QueryBody(question="validate", filters=json.loads(metadata)).filters
        except (ValueError, TypeError):
            raise Problem(422, "Metadata must be a bounded JSON object of strings") from None
        return service.ingest(p, file.filename or "file.txt", file.file.read(s.max_upload_bytes+1), values)

    @app.get("/v1/documents")
    def listing(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200), p=Depends(principal)):
        return {"items": service.list_documents(p, offset, limit)}

    @app.delete("/v1/documents/{document_id}")
    def remove(document_id: uuid.UUID, p=Depends(principal)):
        return service.remove_document(p, str(document_id))

    @app.post("/v1/documents/{document_id}/retry")
    def retry(document_id: uuid.UUID, p=Depends(principal)):
        return service.retry(p, str(document_id))

    @app.get("/v1/chunks/{chunk_id}")
    def source(chunk_id: uuid.UUID, p=Depends(principal)):
        return service.get_chunk(p, str(chunk_id))

    @app.post("/v1/query")
    def query(body: QueryBody, p=Depends(principal)):
        return service.query(p, body.question, str(body.conversation_id) if body.conversation_id else None,
                             body.filters, body.top_k, body.quality)

    @app.get("/v1/conversations/{conversation_id}")
    def conversation(conversation_id: uuid.UUID, p=Depends(principal)):
        return service.conversation(p, str(conversation_id))

    @app.delete("/v1/conversations/{conversation_id}")
    def erase_conversation(conversation_id: uuid.UUID, p=Depends(principal)):
        return service.conversation(p, str(conversation_id), remove=True)

    @app.get("/v1/stats")
    def stats(p=Depends(principal)):
        return service.stats(p)

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(static / "index.html")

    return app

