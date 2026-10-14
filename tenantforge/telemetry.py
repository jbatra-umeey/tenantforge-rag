import logging
import os

from opentelemetry import trace
from prometheus_client import Counter, Histogram

REQUESTS = Counter("tenantforge_http_requests", "HTTP responses", ["route", "status"])
LATENCY = Histogram("tenantforge_http_seconds", "HTTP request duration", ["route"],
                    buckets=(.01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10, 30, 60))
JOBS = Counter("tenantforge_jobs", "Worker job outcomes", ["outcome"])
STAGE = Histogram("tenantforge_stage_seconds", "RAG stage latency", ["stage"])
CACHE = Counter("tenantforge_cache", "Answer cache outcomes", ["outcome"])
TRACER = trace.get_tracer("tenantforge")
_configured = False


def configure(settings):
    global _configured
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(message)s")
    if settings.otlp_endpoint and not _configured:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        provider = TracerProvider(resource=Resource.create({"service.name": "tenantforge"}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otlp_endpoint)))
        trace.set_tracer_provider(provider)
        _configured = True

