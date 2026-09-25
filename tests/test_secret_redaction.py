"""F33: Telegram bot token / Polygon apiKey must not reach logs or the dead-letter queue."""
import json
import logging
import sys

import pytest
import requests

from shared.redaction import MASK, RedactingFilter, install_log_redaction, redact, redact_exc

TG_TOKEN = "7654321:AAH-fake_tokenValue123"
PG_KEY = "PgFakeKey0123456789"


def _tg_error():
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    return requests.exceptions.ConnectionError(
        f"HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded "
        f"with url: /bot{TG_TOKEN}/sendMessage (Caused by NewConnectionError('{url}'))"
    )


def _pg_error():
    return requests.exceptions.ConnectionError(
        "HTTPSConnectionPool(host='api.polygon.io', port=443): Max retries exceeded with url: "
        f"/v2/aggs/ticker/WOLF/range/1/day/2025-01-01/2025-02-01?adjusted=true&sort=asc"
        f"&limit=5000&apiKey={PG_KEY} (Caused by NewConnectionError('x'))"
    )


# ── redact() ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, secret", [
    (f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", TG_TOKEN),
    ("https://api.telegram.org/botNoColonToken/getMe", "NoColonToken"),
    (f"/v2/aggs?adjusted=true&apiKey={PG_KEY}&x=1", PG_KEY),
    ("?api_key=abc123def", "abc123def"),
    ("?token=tok987654", "tok987654"),
    ("?key=kkk555&page=2", "kkk555"),
    ("Authorization: Bearer eyJhbGciOi.abc.def", "eyJhbGciOi.abc.def"),
    ("{'Authorization': 'Bearer s3cr3tvalue'}", "s3cr3tvalue"),
    ("{'APCA-API-SECRET-KEY': 'alpacaSecret99'}", "alpacaSecret99"),
    ("APCA-API-SECRET-KEY: alpacaSecret99", "alpacaSecret99"),
    ('{"api_key": "jsonSecret1"}', "jsonSecret1"),
    ("postgres://user:dbPassw0rd@host:5432/db", "dbPassw0rd"),
])
def test_redact_masks_credentials(text, secret):
    out = redact(text)
    assert secret not in out
    assert MASK in out


def test_redact_keeps_ordinary_text():
    for text in ("KeyError: 'symbol'", "/bottom/of/path", "monkey business",
                 "WOLF UP 72% confident", "HTTP 429: Too Many Requests"):
        assert redact(text) == text


def test_redact_masks_secret_env_values_in_any_shape(monkeypatch):
    monkeypatch.setenv("SOME_PROVIDER_API_KEY", "literal-secret-value-xyz")
    assert "literal-secret-value-xyz" not in redact("weird shape literal-secret-value-xyz here")


def test_redact_exc_keeps_type_name():
    out = redact_exc(_tg_error())
    assert out.startswith("ConnectionError: ")
    assert TG_TOKEN not in out


# ── logging filter ──────────────────────────────────────────────────────────

class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


def _logger_with_filter(name):
    handler = _ListHandler()
    handler.addFilter(RedactingFilter())
    logger = logging.getLogger(name)
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger, handler


def test_filter_redacts_args_message_and_traceback():
    logger, handler = _logger_with_filter("test.redaction.filter")
    err = _pg_error()
    logger.warning("Polygon %s: %s", "WOLF", err)
    logger.error(f"inline {err}")
    try:
        raise _tg_error()
    except requests.exceptions.ConnectionError:
        logger.exception("send failed")
    text = "\n".join(handler.lines)
    assert PG_KEY not in text
    assert TG_TOKEN not in text
    assert "Polygon WOLF:" in text
    assert "Traceback" in text


def test_filter_keeps_numeric_args_and_tuple_shape():
    logger, handler = _logger_with_filter("test.redaction.tuple")
    record_args = []

    class _Peek(logging.Filter):
        def filter(self, record):
            record_args.append(record.args)
            return True

    handler.addFilter(_Peek())
    logger.info('%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "GET", "/x?token=abc123", "1.1", 200)
    assert handler.lines == ['1.2.3.4:5 - "GET /x?token=*** HTTP/1.1" 200']
    assert isinstance(record_args[0], tuple) and record_args[0][-1] == 200


def test_install_log_redaction_is_idempotent():
    root = logging.getLogger()
    handler = _ListHandler()
    root.addHandler(handler)
    try:
        install_log_redaction()
        install_log_redaction()
        assert sum(isinstance(f, RedactingFilter) for f in handler.filters) == 1
    finally:
        root.removeHandler(handler)


def test_uvicorn_log_config_wires_the_filter():
    from pathlib import Path
    with open(Path(__file__).resolve().parent.parent / "uvicorn_log.json") as fh:
        cfg = json.load(fh)
    assert cfg["filters"]["redact_secrets"]["()"] == "shared.redaction.RedactingFilter"
    for name in ("default", "access"):
        assert "redact_secrets" in cfg["handlers"][name]["filters"]


# ── core.telegram: logs + dead-letter queue ─────────────────────────────────

class _DeadLetterDb:
    def __init__(self):
        self.writes = []

    def __call__(self):
        db = self

        class _Cur:
            def execute(self, sql, params=None):
                if params and "telegram_dead_letter" in sql:
                    db.writes.append(params[0])

            def fetchone(self):
                return None

        class _Conn:
            def cursor(self):
                return _Cur()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _Conn()


def test_telegram_connection_error_is_redacted_in_logs_and_dead_letter(monkeypatch, caplog):
    import core.db
    import core.telegram as tg

    monkeypatch.setattr(tg, "BOT_TOKEN", TG_TOKEN)
    monkeypatch.setattr(tg, "CHAT_ID", "42")
    monkeypatch.setattr(tg, "DISCORD_URL", "")
    monkeypatch.setattr(tg, "ALERTS_ENABLED", True)
    monkeypatch.setattr(tg, "_TELEGRAM_RETRIES", 2)
    monkeypatch.setattr(tg.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(tg, "ensure_ghost_state", lambda cur: None)

    def _boom(url, **kw):
        assert TG_TOKEN in url  # the real request still carries the token
        raise _tg_error()

    monkeypatch.setattr(tg.requests, "post", _boom)
    fake_db = _DeadLetterDb()
    monkeypatch.setattr(core.db, "db_conn", fake_db)

    caplog.set_level(logging.DEBUG)
    body = f"health: polygon failed url ...&apiKey={PG_KEY}"
    assert tg._send(body) is False

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "ConnectionError" in logged
    assert TG_TOKEN not in logged
    assert fake_db.writes, "dead-letter row was not written"
    stored = "\n".join(fake_db.writes)
    assert TG_TOKEN not in stored
    assert PG_KEY not in stored
    entry = json.loads(fake_db.writes[-1])[-1]
    assert "ConnectionError" in entry["error"]


def test_enqueue_dead_letter_redacts_directly(monkeypatch):
    import core.db
    import core.telegram as tg

    monkeypatch.setattr(tg, "ensure_ghost_state", lambda cur: None)
    fake_db = _DeadLetterDb()
    monkeypatch.setattr(core.db, "db_conn", fake_db)
    tg._enqueue_dead_letter("text", str(_tg_error()))
    assert fake_db.writes and TG_TOKEN not in fake_db.writes[-1]


# ── Polygon OHLCV path ──────────────────────────────────────────────────────

def test_polygon_connection_error_is_redacted_in_logs(monkeypatch, caplog):
    import core.signal_engine as se

    monkeypatch.setenv("POLYGON_API_KEY", PG_KEY)

    def _boom(url, **kw):
        raise _pg_error()

    monkeypatch.setattr(requests, "get", _boom)
    caplog.set_level(logging.DEBUG)
    assert se._try_polygon_ohlcv("WOLF", "1y") is None
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "ConnectionError" in logged
    assert PG_KEY not in logged


def test_edge_notify_redact_delegates_to_shared():
    from edge import notify as N
    assert TG_TOKEN not in N.redact(str(_tg_error()))
    assert PG_KEY not in N.redact(str(_pg_error()))
    assert "edge.notify" in sys.modules
