"""My Picks — server-persisted personal watchlist (auth-gated CRUD + summaries)."""
import core.my_picks as mp


class _Cur:
    def __init__(self, count=0, rows=None):
        self.count = count
        self.rows = rows or []
        self.executed = []
        self.rowcount = 1
        self._last = ""

    def execute(self, sql, params=None):
        self._last = sql
        self.executed.append((sql, params))

    def fetchone(self):
        if "COUNT(*)" in self._last:
            return (self.count,)
        return None

    def fetchall(self):
        return list(self.rows)


# ── symbol validation ────────────────────────────────────────────────────

def test_clean_symbol_accepts_real_tickers():
    assert mp.clean_symbol(" aapl ") == "AAPL"
    assert mp.clean_symbol("BRK.B") == "BRK.B"
    assert mp.clean_symbol("bf-b") == "BF-B"


def test_clean_symbol_rejects_garbage():
    assert mp.clean_symbol("") is None
    assert mp.clean_symbol("1BAD") is None            # must start with a letter
    assert mp.clean_symbol("WAYTOOLONGSYM") is None   # > 8 chars
    assert mp.clean_symbol("AA PL") is None
    assert mp.clean_symbol("<script>") is None
    assert mp.clean_symbol(None) is None


# ── CRUD ─────────────────────────────────────────────────────────────────

def test_add_symbol_inserts_upper_with_conflict_guard():
    cur = _Cur(count=0)
    out = mp.add_symbol(cur, "nvda", note="my core holding")
    assert out["ok"] is True and out["symbol"] == "NVDA" and out["added"] is True
    ins = [s for s, _ in cur.executed if "INSERT INTO user_my_picks" in s]
    assert ins and "ON CONFLICT (symbol) DO NOTHING" in ins[0]


def test_add_symbol_rejects_invalid_and_respects_cap():
    cur = _Cur(count=0)
    assert mp.add_symbol(cur, "no$good")["ok"] is False
    full = _Cur(count=mp.MAX_PICKS)
    out = mp.add_symbol(full, "AAPL")
    assert out["ok"] is False and "limit" in out["error"]


def test_remove_symbol_deletes_by_clean_symbol():
    cur = _Cur()
    out = mp.remove_symbol(cur, " tsla ")
    assert out["ok"] is True and out["symbol"] == "TSLA" and out["removed"] is True
    assert any("DELETE FROM user_my_picks" in s for s, _ in cur.executed)


def test_list_symbols_shapes_rows():
    cur = _Cur(rows=[("NVDA", "core", 1700000000), ("WOLF", None, 1700000100)])
    out = mp.list_symbols(cur)
    assert out[0] == {"symbol": "NVDA", "note": "core", "added_at": 1700000000}
    assert out[1]["note"] == ""   # NULL note normalizes to ""


# ── auth gating (route contract) ─────────────────────────────────────────

def test_my_picks_auth_is_env_gated():
    """PR #146: My picks is public by default (single-operator dashboard, the
    operator asked to view without /admin login) but every route routes auth
    through _my_picks_gated so MY_PICKS_REQUIRE_AUTH=1 restores the gate."""
    import inspect
    import core.portfolio_routes as pr
    for fn in (pr.get_my_picks, pr.add_my_pick, pr.delete_my_pick):
        assert "_my_picks_gated" in inspect.getsource(fn), fn.__name__
    # the gate helper honors the env flag and only then requires portfolio auth
    gate_src = inspect.getsource(pr._my_picks_gated)
    assert "MY_PICKS_REQUIRE_AUTH" in gate_src
    assert "require_portfolio_auth" in gate_src


def test_my_picks_public_by_default(monkeypatch):
    """With the flag unset, the gate is a no-op — no auth required to view."""
    import core.portfolio_routes as pr
    monkeypatch.delenv("MY_PICKS_REQUIRE_AUTH", raising=False)
    pr._my_picks_gated(object())  # must not raise even with a bogus request


def test_my_picks_routes_registered():
    import core.portfolio_routes as pr
    # GET and POST on the same path are separate route objects — aggregate
    # methods per path instead of letting a dict comprehension clobber them.
    paths: dict = {}
    for r in pr.portfolio_router.routes:
        p = getattr(r, "path", None)
        for m in (getattr(r, "methods", []) or []):
            paths.setdefault(p, set()).add(m)
    assert "GET" in paths.get("/api/my-picks", set())
    assert "POST" in paths.get("/api/my-picks", set())
    assert "DELETE" in paths.get("/api/my-picks/{symbol}", set())
