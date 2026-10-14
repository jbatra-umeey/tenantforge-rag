import os
from dataclasses import dataclass, fields

DEV_SECRET = "local-demo-only-replace-before-sharing-32-bytes"


@dataclass(frozen=True)
class Settings:
    environment: str = "development"
    database_url: str = "sqlite:///./data/tenantforge.db"
    storage_dir: str = "./data/objects"
    storage_backend: str = "local"
    s3_bucket: str = ""
    s3_endpoint: str = ""
    auth_mode: str = "local"
    jwt_secret: str = DEV_SECRET
    jwt_issuer: str = "tenantforge-local"
    jwt_audience: str = "tenantforge-api"
    jwks_url: str = ""
    embedding_backend: str = "hash"
    embedding_model: str = "hash-v1"
    embedding_dimensions: int = 384
    embedding_batch_size: int = 32
    generation_backend: str = "extractive"
    llm_model: str = ""
    llm_complex_model: str = ""
    provider_base_url: str = "https://api.openai.com/v1"
    provider_api_key: str = ""
    reranker: str = "lexical"
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    max_upload_bytes: int = 10 * 1024 * 1024
    max_extracted_chars: int = 1_000_000
    max_chunks: int = 2000
    max_pending_jobs: int = 100
    max_documents: int = 1000
    max_attempts: int = 3
    lease_seconds: int = 120
    query_per_minute: int = 60
    daily_token_budget: int = 1_000_000
    max_context_chars: int = 12000
    max_history_chars: int = 6000
    max_output_tokens: int = 600
    cache_ttl_seconds: int = 120
    sqlite_max_chunks: int = 20000
    metrics_token: str = ""
    otlp_endpoint: str = ""

    @classmethod
    def from_env(cls):
        data = {}
        for f in fields(cls):
            value = os.getenv(f"TF_{f.name.upper()}")
            if value is not None:
                data[f.name] = int(value) if f.type is int else value
        result = cls(**data)
        result.validate()
        return result

    @property
    def fingerprint(self):
        return f"{self.embedding_backend}:{self.embedding_model}:{self.embedding_dimensions}"

    def validate(self):
        if self.embedding_backend not in {"hash", "openai", "sentence_transformers"}:
            raise ValueError("Unknown embedding backend")
        if self.generation_backend not in {"extractive", "openai"}:
            raise ValueError("Unknown generation backend")
        if self.storage_backend not in {"local", "s3"} or self.auth_mode not in {"local", "oidc"}:
            raise ValueError("Invalid storage or auth mode")
        if self.reranker not in {"lexical", "cross_encoder"}:
            raise ValueError("Invalid reranker")
        for f in fields(self):
            if f.type is int and getattr(self, f.name) <= 0:
                raise ValueError(f"{f.name} must be positive")
        if self.embedding_dimensions > 2000:
            raise ValueError("This HNSW vector schema supports dimensions <= 2000")
        if self.auth_mode == "oidc" and not self.jwks_url.startswith("https://"):
            raise ValueError("OIDC requires an HTTPS JWKS URL")
        if self.storage_backend == "s3" and not self.s3_bucket:
            raise ValueError("S3 bucket is required")
        if "openai" in {self.embedding_backend, self.generation_backend} and not self.provider_api_key:
            raise ValueError("Configure TF_PROVIDER_API_KEY for the provider backend")
        if self.generation_backend == "openai" and not self.llm_model:
            raise ValueError("Configure TF_LLM_MODEL")
        if self.embedding_backend == "openai" and self.embedding_model == "hash-v1":
            raise ValueError("Configure TF_EMBEDDING_MODEL")
        if self.environment == "production":
            if not self.database_url.startswith("postgresql"):
                raise ValueError("Production requires PostgreSQL")
            if self.auth_mode != "oidc" or self.storage_backend != "s3" or not self.metrics_token:
                raise ValueError("Production requires OIDC, S3 and a metrics token")
            if self.embedding_backend == "hash":
                raise ValueError("Hash embeddings are a local demonstration mode")

