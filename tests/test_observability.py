"""Structured JSON logging."""

from __future__ import annotations

import json
import logging
import sys

from app.observability import JsonFormatter, configure_logging, request_id_var

# --- Structured logging --------------------------------------------------------------------


def test_json_formatter_includes_request_id_fields_and_exceptions():
    token = request_id_var.set("req-42")
    try:
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            record = logging.getLogger("t").makeRecord(
                "t",
                logging.ERROR,
                __file__,
                1,
                "failed %s",
                ("x",),
                sys.exc_info(),
                extra={"fields": {"route": "/p"}},
            )
        entry = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(token)
    assert entry["message"] == "failed x"
    assert entry["level"] == "error"
    assert entry["request_id"] == "req-42"
    assert entry["route"] == "/p"
    assert "RuntimeError: boom" in entry["exception"]
    assert entry["ts"].endswith("+00:00")


def test_json_formatter_plain_record():
    record = logging.getLogger("t").makeRecord("t", logging.INFO, __file__, 1, "hello", (), None)
    entry = json.loads(JsonFormatter().format(record))
    assert entry == {**entry, "message": "hello", "level": "info", "request_id": "-"}
    assert "exception" not in entry


def test_configure_logging_installs_json_handler():
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        configure_logging(logging.DEBUG)
        assert root.level == logging.DEBUG
        assert [type(h.formatter) for h in root.handlers] == [JsonFormatter]
    finally:
        root.handlers[:], root.level = saved
