from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_no_default_api_auth_token_in_startup_self_call():
    text = (ROOT / "engines" / "startup.py").read_text(encoding="utf-8")
    assert 'API_AUTH_TOKEN", "ghost-prod-2024"' not in text
    assert "API_AUTH_TOKEN is not configured" in text


def test_no_raw_private_key_or_secret_assignment_in_public_html():
    for rel in ("ghost_console.html", "admin.html", "cockpit.html", "picks.html"):
        text = (ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        assert "BEGIN PRIVATE KEY" not in text
        assert "CRON_SECRET=" not in text
        assert "sk_live_" not in text
