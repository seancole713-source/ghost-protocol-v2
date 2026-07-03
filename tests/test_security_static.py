from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# PR #125: engines/startup.py was dead code (never imported, self-declared
# deprecated). Removed. The startup auth guard now lives in wolf_app.py
# lines 39-42 (CRON_SECRET required in production).


def test_no_raw_private_key_or_secret_assignment_in_public_html():
    for rel in ("ghost_console.html", "admin.html", "cockpit.html", "picks.html"):
        text = (ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        assert "BEGIN PRIVATE KEY" not in text
        assert "CRON_SECRET=" not in text
        assert "sk_live_" not in text
