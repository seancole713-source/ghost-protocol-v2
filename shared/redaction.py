"""shared/redaction.py -- scrub credentials out of text before it is logged or stored.

Network exceptions from ``requests`` embed the full request URL in their
message, so ``str(exc)`` for a Telegram call carries ``/bot<TOKEN>/`` and a
Polygon call carries ``apiKey=<KEY>``. Anything that logs or persists exception
text must pass it through :func:`redact` first. :class:`RedactingFilter` does
the same for every log record that reaches a handler it is attached to.

Stdlib only. Imported by core/ and edge/ alike.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Iterable

MASK = "***"

# /bot<token>/sendMessage  (Telegram puts the bot token in the URL path)
# Token-shaped (<digits>:<secret>) after any /bot, or anything after
# api.telegram.org/bot (a malformed token is still a token).
_BOT = re.compile(
    r"(?i)(api\.telegram\.org/bot|/bot(?=[^/\s'\"?#]*:))[^/\s'\"?#]+"
)

# query-string / form / kwargs style: apiKey=..., api_key=..., token=..., key=...
_PARAM = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"((?:access_|refresh_|client_|auth_|id_|bot_|session_|private_)?"
    r"(?:api[_-]?key|apikey|token|secret|secret[_-]?key|key|password|passwd|signature|sig)"
    r"\s*=\s*)"
    r"[^&\s'\",;)}\]]+"
)

# JSON / dict repr: "api_key": "...", 'token': '...'
_QUOTED = re.compile(
    r"(?i)(['\"](?:access_|refresh_|client_|auth_)?"
    r"(?:api[_-]?key|apikey|token|secret|secret[_-]?key|password|passwd)['\"]\s*:\s*['\"])"
    r"[^'\"]+"
)

# Header-shaped secrets: Authorization, X-Api-Key, Alpaca, cron secret.
_HEADER = re.compile(
    r"(?i)(['\"]?(?:authorization|proxy-authorization|x-api-key|apca-api-secret-key|"
    r"apca-api-key-id|x-cron-secret|x-ghost-cron-secret)['\"]?\s*[:=]\s*['\"]?)"
    r"(?:(bearer|basic|token)\s+)?"
    r"[^\s'\",;}]+"
)

# Credentials in a URL's userinfo: scheme://user:password@host
_USERINFO = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^:/\s@]+:)[^@\s/]+(@)")

# A bare bearer token anywhere ("Bearer abc.def").
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{6,}")

# Env vars whose values are secrets: their literal values are masked wherever
# they appear, even in shapes the patterns above do not know.
_SECRET_ENV_NAME = re.compile(
    r"(?i)(TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|APIKEY|SECRET_KEY|_KEY|_KEY_ID)$"
)
_MIN_SECRET_LEN = 12


def _secret_env_values(environ: Any = None) -> Iterable[str]:
    env = os.environ if environ is None else environ
    vals = []
    for name, value in list(env.items()):
        if not value or len(value) < _MIN_SECRET_LEN:
            continue
        if _SECRET_ENV_NAME.search(name):
            vals.append(value)
    # longest first so a value that contains another is masked whole
    return sorted(set(vals), key=len, reverse=True)


def _header_sub(m: "re.Match[str]") -> str:
    scheme = m.group(2)
    return m.group(1) + (scheme + " " if scheme else "") + MASK


def redact(text: Any) -> str:
    """Return ``str(text)`` with credentials masked. Never raises."""
    try:
        s = str(text)
    except Exception:  # noqa: BLE001 - redaction must never break a log path
        return "<unprintable>"
    try:
        for value in _secret_env_values():
            if value in s:
                s = s.replace(value, MASK)
        s = _BOT.sub(r"\1" + MASK, s)
        s = _USERINFO.sub(r"\1" + MASK + r"\2", s)
        s = _HEADER.sub(_header_sub, s)
        s = _QUOTED.sub(r"\1" + MASK, s)
        s = _PARAM.sub(r"\1" + MASK, s)
        s = _BEARER.sub(r"\1" + MASK, s)
    except Exception:  # noqa: BLE001
        return "<redaction failed>"
    return s


def redact_exc(exc: BaseException, limit: int = 200) -> str:
    """``TypeName: redacted message`` trimmed to ``limit`` characters."""
    return (type(exc).__name__ + ": " + redact(exc))[:limit]


class RedactingFilter(logging.Filter):
    """Logging filter that masks credentials in the message and traceback.

    Attach it to HANDLERS (logger-level filters are skipped for records that
    propagate up from child loggers). It never drops a record.
    """

    _fmt = logging.Formatter()

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            self._redact_message(record)
        except Exception:  # noqa: BLE001 - never break logging
            pass
        if record.exc_info and not record.exc_text:
            try:
                record.exc_text = self._fmt.formatException(record.exc_info)
            except Exception:  # noqa: BLE001
                record.exc_text = None
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if getattr(record, "stack_info", None):
            record.stack_info = redact(record.stack_info)
        return True

    @staticmethod
    def _redact_message(record: logging.LogRecord) -> None:
        msg = record.msg if isinstance(record.msg, str) else redact(record.msg)
        args = record.args
        if not args:
            record.msg = redact(msg)
            return
        record.msg = msg
        if isinstance(args, tuple) and redact(msg) == msg:
            # Keep the tuple shape: some formatters (uvicorn's access log)
            # unpack record.args positionally. Numbers stay numbers so %d/%f
            # still work; everything else becomes a redacted string.
            new_args = tuple(
                a if isinstance(a, (int, float)) or a is None else redact(a)
                for a in args
            )
            record.args = new_args
            try:
                record.getMessage()
                return
            except Exception:  # noqa: BLE001 - e.g. %r / %x on a converted arg
                record.args = args
        # Fallback: format with the original args, then redact the result.
        record.msg = redact(record.getMessage())
        record.args = None


def _attach(handler: logging.Handler) -> None:
    if not any(isinstance(f, RedactingFilter) for f in handler.filters):
        handler.addFilter(RedactingFilter())


def install_log_redaction(extra_loggers: Iterable[str] = ("uvicorn", "uvicorn.error", "uvicorn.access")) -> None:
    """Attach :class:`RedactingFilter` to the root handlers and the named loggers' handlers.

    Idempotent. Call after logging is configured.
    """
    for handler in logging.getLogger().handlers:
        _attach(handler)
    for name in extra_loggers:
        for handler in logging.getLogger(name).handlers:
            _attach(handler)
