"""WOLF monitor Telegram bucket tests."""


def test_price_move_bucket_key_groups_same_down_move():
    import core.wolf_monitor as wm
    assert wm._price_move_bucket_key(-8.8) == "price_move_DOWN_8"
    assert wm._price_move_bucket_key(-8.9) == "price_move_DOWN_8"
    assert wm._price_move_bucket_key(-10.1) == "price_move_DOWN_10"
    assert wm._price_move_bucket_key(5.6) == "price_move_UP_5"


def test_maybe_send_uses_long_bucket_cooldown(monkeypatch):
    import core.wolf_monitor as wm
    sent = []
    monkeypatch.setattr(wm, "PRICE_MOVE_BUCKET_COOLDOWN", 86400)
    monkeypatch.setattr(wm, "_send", lambda key, message, cooldown_s=None: sent.append((key, cooldown_s)))
    monkeypatch.setattr(wm.time, "time", lambda: 1000)
    wm._last_alert.clear()
    wm._maybe_send("price_move_DOWN_8", "msg")
    wm._maybe_send("price_move_DOWN_8", "msg2")
    assert sent == [("price_move_DOWN_8", 86400)]
