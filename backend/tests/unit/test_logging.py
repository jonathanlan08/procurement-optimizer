"""Unit tests for structured JSON logging (stdlib only)."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from datetime import datetime

import pytest

from app.core.logging import JsonFormatter, configure_logging


@pytest.fixture(autouse=True)
def _isolated_root_logger() -> Iterator[None]:
    """Snapshot and restore the root logger so tests never leak handlers."""
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    root.handlers = []
    yield
    root.handlers = handlers
    root.setLevel(level)


def _json_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if isinstance(h.formatter, JsonFormatter)]


def _log_one(capsys: pytest.CaptureFixture[str], **extra: object) -> dict[str, object]:
    configure_logging()
    logging.getLogger("test.logger").info("hit", extra=extra)
    out = capsys.readouterr().out.strip()
    payload: dict[str, object] = json.loads(out)
    return payload


class TestConfigureLogging:
    def test_installs_a_single_json_handler(self) -> None:
        configure_logging()
        assert len(_json_handlers()) == 1

    def test_is_idempotent(self) -> None:
        configure_logging()
        configure_logging()
        configure_logging()
        assert len(_json_handlers()) == 1

    def test_updates_level_without_duplicating_handlers(self) -> None:
        configure_logging("INFO")
        configure_logging("DEBUG")
        handlers = _json_handlers()
        assert len(handlers) == 1
        assert logging.getLogger().level == logging.DEBUG
        assert handlers[0].level == logging.DEBUG

    def test_handler_writes_to_stdout(self) -> None:
        configure_logging()
        handler = _json_handlers()[0]
        assert isinstance(handler, logging.StreamHandler)
        assert handler.stream is sys.stdout


class TestJsonOutputShape:
    def test_emits_one_json_object_per_line(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging()
        logging.getLogger("test.logger").info("hello world")
        out = capsys.readouterr().out.strip()
        lines = out.splitlines()
        assert len(lines) == 1
        payload = json.loads(lines[0])
        assert payload["message"] == "hello world"
        assert payload["logger"] == "test.logger"
        assert payload["level"] == "INFO"

    def test_ts_is_iso8601_utc(self, capsys: pytest.CaptureFixture[str]) -> None:
        payload = _log_one(capsys)
        parsed = datetime.fromisoformat(str(payload["ts"]))
        assert parsed.utcoffset() is not None
        assert parsed.utcoffset().total_seconds() == 0  # type: ignore[union-attr]

    def test_includes_extra_fields(self, capsys: pytest.CaptureFixture[str]) -> None:
        payload = _log_one(capsys, request_id="abc-123")
        assert payload["request_id"] == "abc-123"

    def test_multiple_records_are_separate_lines(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging()
        logger = logging.getLogger("test.logger")
        logger.info("first")
        logger.info("second")
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["message"] == "first"
        assert json.loads(lines[1])["message"] == "second"


class TestSecretRedaction:
    @pytest.mark.parametrize(
        "key",
        [
            "password",
            "api_token",
            "client_secret",
            "api_key",
            "Authorization",
            "session_cookie",
        ],
    )
    def test_redacts_secretish_extra_keys(
        self, capsys: pytest.CaptureFixture[str], key: str
    ) -> None:
        payload = _log_one(capsys, **{key: "super-sensitive-value"})
        assert payload[key] == "[redacted]"

    def test_does_not_redact_unrelated_extras(self, capsys: pytest.CaptureFixture[str]) -> None:
        payload = _log_one(capsys, user_id="u-1")
        assert payload["user_id"] == "u-1"


class TestExceptionLogging:
    def test_exception_includes_stack(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging()
        logger = logging.getLogger("test.logger")
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("failed")
        payload = json.loads(capsys.readouterr().out.strip())
        assert "stack" in payload
        assert "ValueError: boom" in payload["stack"]
        assert "Traceback" in payload["stack"]

    def test_no_stack_key_without_exception(self, capsys: pytest.CaptureFixture[str]) -> None:
        payload = _log_one(capsys)
        assert "stack" not in payload
