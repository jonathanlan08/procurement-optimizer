"""Typed application settings.

Fail-fast: invalid combinations refuse to start rather than degrade silently.
Never log or serialize secret values.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEV = "dev"
    TEST = "test"
    PROD = "prod"


class ExtractionProviderKind(StrEnum):
    MOCK = "mock"
    ANTHROPIC = "anthropic"


class OcrProviderKind(StrEnum):
    MOCK = "mock"


class StorageProviderKind(StrEnum):
    FILESYSTEM = "filesystem"
    S3 = "s3"


class NarrativeProviderKind(StrEnum):
    TEMPLATE = "template"
    ANTHROPIC = "anthropic"


class JobRunnerKind(StrEnum):
    INLINE = "inline"  # synchronous, used in tests and simple deployments
    THREAD = "thread"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Environment = Environment.DEV
    database_url: str = Field(
        default="postgresql+psycopg://postgres:postgres@localhost:5432/procurement",
        description="SQLAlchemy URL; tests may override with a pgserver socket URL",
    )
    session_ttl_hours: int = 12
    secret_key: SecretStr = Field(
        default=SecretStr("dev-only-secret-change-me"),
        description="Signs nothing secret in v0.1 but must be set in prod",
    )
    cookie_secure: bool = False  # forced True in prod by the validator
    allowed_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    max_upload_bytes: int = 20 * 1024 * 1024  # 20 MiB
    rate_limit_per_minute: int = 120
    rate_limit_auth_per_minute: int = 10

    extraction_provider: ExtractionProviderKind = ExtractionProviderKind.MOCK
    ocr_provider: OcrProviderKind = OcrProviderKind.MOCK
    narrative_provider: NarrativeProviderKind = NarrativeProviderKind.TEMPLATE
    storage_provider: StorageProviderKind = StorageProviderKind.FILESYSTEM
    job_runner: JobRunnerKind = JobRunnerKind.THREAD

    anthropic_api_key: SecretStr | None = None
    storage_root: str = ".local-storage"  # filesystem provider root (dev)
    s3_endpoint_url: str | None = None
    s3_bucket: str | None = None
    s3_access_key: SecretStr | None = None
    s3_secret_key: SecretStr | None = None

    demo_mode: bool = True  # demo banner + seeded demo organization

    # Directory holding the built frontend (frontend/dist). When set, this
    # process also serves the SPA, so the browser talks to ONE origin: the
    # frontend calls the API with relative paths and the session cookie is
    # SameSite=Lax, neither of which survives a split across two domains.
    # Unset (the default) leaves this an API-only server, which is what the
    # dev setup and the whole test suite expect.
    static_root: str | None = None

    @model_validator(mode="after")
    def _fail_fast(self) -> Self:
        needs_key = (
            self.extraction_provider is ExtractionProviderKind.ANTHROPIC
            or self.narrative_provider is NarrativeProviderKind.ANTHROPIC
        )
        if needs_key and self.anthropic_api_key is None:
            raise ValueError(
                "extraction/narrative provider 'anthropic' requires PO_ANTHROPIC_API_KEY"
            )
        if self.storage_provider is StorageProviderKind.S3:
            missing = [
                name
                for name, val in (
                    ("PO_S3_ENDPOINT_URL", self.s3_endpoint_url),
                    ("PO_S3_BUCKET", self.s3_bucket),
                    ("PO_S3_ACCESS_KEY", self.s3_access_key),
                    ("PO_S3_SECRET_KEY", self.s3_secret_key),
                )
                if val is None
            ]
            if missing:
                raise ValueError(f"storage provider 's3' requires: {', '.join(missing)}")
        if self.environment is Environment.PROD:
            if self.secret_key.get_secret_value() == "dev-only-secret-change-me":
                raise ValueError("PO_SECRET_KEY must be set in prod")
            object.__setattr__(self, "cookie_secure", True)
        return self


def load_settings() -> Settings:
    """Single construction point; import-time singletons are forbidden."""
    return Settings()
