"""Settings validation for deployed environments."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Environment, Settings

NEON_STYLE_URL = "postgresql+psycopg://user:pw@db.example.com/app?sslmode=require"


def _prod(**overrides: object) -> Settings:
    fields: dict[str, object] = {
        "environment": Environment.PROD,
        "secret_key": SecretStr("a-real-secret"),
        "database_url": NEON_STYLE_URL,
    }
    fields.update(overrides)
    return Settings(**fields)  # type: ignore[arg-type]


def test_prod_refuses_the_built_in_localhost_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without PO_DATABASE_URL the app would crash later with an opaque
    'connection refused' on 127.0.0.1; fail at startup naming the variable."""
    monkeypatch.delenv("PO_DATABASE_URL", raising=False)
    with pytest.raises(ValidationError, match="PO_DATABASE_URL must be set in prod"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            environment=Environment.PROD,
            secret_key=SecretStr("a-real-secret"),
        )


def test_prod_accepts_an_explicit_database_url() -> None:
    assert _prod().database_url == NEON_STYLE_URL


def test_trailing_newline_from_a_pasted_value_is_stripped() -> None:
    settings = _prod(database_url=NEON_STYLE_URL + "\n")
    assert settings.database_url == NEON_STYLE_URL


@pytest.mark.parametrize("scheme", ["postgresql://", "postgres://"])
def test_provider_style_urls_are_mapped_to_the_psycopg_driver(scheme: str) -> None:
    """Neon and friends hand out plain URLs, which SQLAlchemy would route to
    psycopg2 - not installed here - and the app would crash on boot."""
    settings = _prod(database_url=scheme + "user:pw@db.example.com/app")
    assert settings.database_url == "postgresql+psycopg://user:pw@db.example.com/app"


def test_dev_keeps_the_local_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PO_DATABASE_URL", raising=False)
    settings = Settings(_env_file=None, environment=Environment.DEV)  # type: ignore[call-arg]
    assert "localhost" in settings.database_url
