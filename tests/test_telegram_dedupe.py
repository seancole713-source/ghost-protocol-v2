"""Telegram persistent de-dupe: prevents restart/redeploy alert spam."""
import json


def test_send_telegram_message_once_suppresses_duplicate(monkeypatch):
    import core.telegram as tg

    state = {}
    sent = []

    class Cur:
        last = ""
        def execute(self, sql, params=None):
            self.last = sql
            if "INSERT INTO ghost_state" in sql:
                state[params[0]] = params[1]
        def fetchone(self):
            if tg._DEDUPE_STATE_KEY in state:
                return (state[tg._DEDUPE_STATE_KEY],)
            return None
    class Conn:
        def cursor(self): return Cur()
    class Ctx:
        def __enter__(self): return Conn()
        def __exit__(self, *a): return False

    monkeypatch.setattr(tg, "_send", lambda text: sent.append(text) or True)
    monkeypatch.setattr(tg, "ensure_ghost_state", lambda cur: None)
    import core.db as db
    monkeypatch.setattr(db, "db_conn", lambda: Ctx())
    monkeypatch.setattr(tg.time, "time", lambda: 1000)

    assert tg.send_telegram_message_once("k", "first", cooldown_s=3600) is True
    assert tg.send_telegram_message_once("k", "second", cooldown_s=3600) is True
    assert sent == ["first"]
    saved = json.loads(state[tg._DEDUPE_STATE_KEY])
    assert saved["k"] == 1000


def test_send_telegram_message_once_allows_after_cooldown(monkeypatch):
    import core.telegram as tg

    state = {}
    sent = []

    class Cur:
        last = ""
        def execute(self, sql, params=None):
            self.last = sql
            if "INSERT INTO ghost_state" in sql:
                state[params[0]] = params[1]
        def fetchone(self):
            if tg._DEDUPE_STATE_KEY in state:
                return (state[tg._DEDUPE_STATE_KEY],)
            return None
    class Conn:
        def cursor(self): return Cur()
    class Ctx:
        def __enter__(self): return Conn()
        def __exit__(self, *a): return False

    monkeypatch.setattr(tg, "_send", lambda text: sent.append(text) or True)
    monkeypatch.setattr(tg, "ensure_ghost_state", lambda cur: None)
    import core.db as db
    monkeypatch.setattr(db, "db_conn", lambda: Ctx())

    monkeypatch.setattr(tg.time, "time", lambda: 1000)
    tg.send_telegram_message_once("k", "first", cooldown_s=10)
    monkeypatch.setattr(tg.time, "time", lambda: 1011)
    tg.send_telegram_message_once("k", "second", cooldown_s=10)
    assert sent == ["first", "second"]
