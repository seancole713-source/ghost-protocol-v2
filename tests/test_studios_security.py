"""Studios booking write is auth-gated (forensic Tier 0: AD-1/ST-2)."""
import inspect


def test_studios_booking_requires_auth():
    import api.routes_studios as rs
    src = inspect.getsource(rs.add_studio_booking)
    # The gate must run before any DB write.
    assert "_cron_ok" in src
    assert "_admin_token_valid" in src
    assert "403" in src
    assert src.index("_cron_ok") < src.index("db_conn")


def test_studios_booking_has_request_and_secret_params():
    import api.routes_studios as rs
    sig = inspect.signature(rs.add_studio_booking)
    assert "request" in sig.parameters
    assert "x_cron_secret" in sig.parameters


def test_studios_page_escapes_booking_fields():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    src = (root / "studios.html").read_text()
    # An escape helper exists and is applied to the XSS-prone interpolations.
    assert "function esc(" in src
    assert "${esc(b.date)}" in src
    assert "${esc(b.source)}" in src
    assert "${esc(b.status)}" in src
    assert "${esc(c.studio)}" in src
    # The raw, un-escaped interpolations are gone.
    assert "${b.date} · ${b.source}" not in src
    assert ">${b.status}</span>" not in src
