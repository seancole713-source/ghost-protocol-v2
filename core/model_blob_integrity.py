"""core/model_blob_integrity.py — HMAC signing for pickled model blobs.

Model blobs live in the database next to their SHA-256. A SHA stored beside
the payload proves nothing: whoever can write the payload can write the hash,
and ``pickle.loads`` on that payload is code execution in the web process.

This module signs the raw blob with an HMAC keyed by a secret that is kept
OUTSIDE the database (env ``MODEL_BLOB_HMAC_KEY``) and verifies it before any
unpickle.

Behaviour matrix (``verify_model_blob``):

- key unset:                    load allowed (legacy behaviour); one WARNING
                                per process asks the operator to set the key.
- key set, signature valid:     load allowed.
- key set, signature wrong:     refused (``model_hmac_mismatch``).
- key set, signature missing:   refused (``model_hmac_missing``) unless
                                ``MODEL_BLOB_ALLOW_UNSIGNED=1`` — a one-time
                                migration switch that logs a WARNING on every
                                such load. Every save/train/activation signs
                                the blob, so retraining clears the backlog;
                                the switch should be removed afterwards.

The key value is never logged.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import pickle
import threading
from typing import Any, Optional

LOGGER = logging.getLogger("ghost.model_blob_integrity")

HMAC_KEY_ENV = "MODEL_BLOB_HMAC_KEY"
ALLOW_UNSIGNED_ENV = "MODEL_BLOB_ALLOW_UNSIGNED"
# Field name used in model metadata JSON / artifact rows for the signature.
SIGNATURE_FIELD = "model_hmac"
# Domain separation so a signature over a model blob can never be replayed as
# a signature for any other use of the same key.
_DOMAIN = b"ghost-model-blob-v1\x00"

_warn_lock = threading.Lock()
_warned_key_unset = False


class ModelBlobIntegrityError(Exception):
    """Raised when a model blob fails HMAC verification (never unpickled)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _hmac_key() -> Optional[bytes]:
    value = os.getenv(HMAC_KEY_ENV, "")
    value = value.strip() if isinstance(value, str) else ""
    return value.encode("utf-8") if value else None


def hmac_key_configured() -> bool:
    return _hmac_key() is not None


def _allow_unsigned() -> bool:
    return str(os.getenv(ALLOW_UNSIGNED_ENV, "")).strip().lower() in ("1", "true", "yes")


def _warn_key_unset_once() -> None:
    global _warned_key_unset
    with _warn_lock:
        if _warned_key_unset:
            return
        _warned_key_unset = True
    LOGGER.warning(
        "model blob integrity: %s is not set; pickled model blobs are loaded "
        "WITHOUT HMAC verification (a database write can execute code). Set %s "
        "to enable signing and verification.",
        HMAC_KEY_ENV, HMAC_KEY_ENV,
    )


def _reset_warning_state_for_tests() -> None:
    global _warned_key_unset
    with _warn_lock:
        _warned_key_unset = False


def _compute(key: bytes, raw: bytes) -> str:
    return hmac.new(key, _DOMAIN + bytes(raw), hashlib.sha256).hexdigest()


def sign_model_blob(raw: bytes) -> Optional[str]:
    """Return the hex HMAC-SHA256 of ``raw``, or None when no key is set."""
    key = _hmac_key()
    if key is None:
        _warn_key_unset_once()
        return None
    return _compute(key, raw)


def verify_model_blob(raw: bytes, signature: Any, *, context: str = "") -> Optional[str]:
    """Check ``raw`` against ``signature`` BEFORE it is unpickled.

    Returns None when the blob may be unpickled, else a short reason code.
    """
    key = _hmac_key()
    if key is None:
        _warn_key_unset_once()
        return None
    sig = str(signature or "").strip().lower()
    if not sig:
        if _allow_unsigned():
            LOGGER.warning(
                "model blob integrity: LOADING UNSIGNED model blob %s because %s=1. "
                "This is a one-time migration switch; retrain/re-save to sign the "
                "blob and then unset %s.",
                context or "(unknown)", ALLOW_UNSIGNED_ENV, ALLOW_UNSIGNED_ENV,
            )
            return None
        LOGGER.error(
            "model blob integrity: REFUSED unsigned model blob %s (no %s). "
            "Retrain to sign it, or set %s=1 once to migrate.",
            context or "(unknown)", SIGNATURE_FIELD, ALLOW_UNSIGNED_ENV,
        )
        return "model_hmac_missing"
    expected = _compute(key, raw)
    if not hmac.compare_digest(expected, sig):
        LOGGER.error(
            "model blob integrity: REFUSED model blob %s: HMAC mismatch "
            "(blob or signature was altered, or signed with a different key)",
            context or "(unknown)",
        )
        return "model_hmac_mismatch"
    return None


def load_verified_pickle(raw: bytes, signature: Any, *, context: str = "") -> Any:
    """Verify the HMAC of ``raw`` and only then unpickle it.

    Raises ModelBlobIntegrityError when verification fails; the payload is
    never passed to pickle in that case.
    """
    reason = verify_model_blob(raw, signature, context=context)
    if reason is not None:
        raise ModelBlobIntegrityError(reason)
    return pickle.loads(raw)  # noqa: S301 — authenticated above
