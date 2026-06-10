"""Telegram card formatters — Daily Card, Weekly Summary, Silence Mode.

Pure string builders: all data is computed by the caller and passed in as plain
dicts, so the formatting is unit-testable without a DB, model, or network. The
assembly (querying picks, track record, news influence) lives in wolf_app's
scheduler jobs; this module only turns those facts into the message text.

Output uses Telegram HTML parse_mode (<b> for headers), matching core.telegram._send.
"""
import os
from typing import Any, Dict, List, Optional

NL = chr(10)


def conviction_from_confidence(confidence: float) -> str:
    """HIGH/MEDIUM/LOW band for the headline conviction tag."""
    c = float(confidence or 0)
    if c >= 0.90:
        return "HIGH"
    if c >= 0.85:
        return "MEDIUM"
    return "LOW"


def compute_news_influence(confidence: float, confidence_raw: float) -> Dict[str, int]:
    """News influence as the share of the final confidence that the sentiment
    nudge moved it by: |conf - conf_raw| / conf. The model's own opinion
    (confidence_raw) is always the base, so model_pct is never 0 and
    influence_pct is capped below 100 (news adds on top, never replaces)."""
    conf = float(confidence or 0)
    raw = float(confidence_raw if confidence_raw is not None else conf)
    if conf <= 0:
        return {"influence_pct": 0, "model_pct": 100}
    infl = abs(conf - raw) / conf * 100.0
    infl = max(0, min(95, int(round(infl))))   # never 100% news
    return {"influence_pct": infl, "model_pct": 100 - infl}


def _fmt_price(v: Optional[float]) -> str:
    if v is None:
        return "$--"
    return "$" + format(float(v), ".2f")


def _signed_pct(v: Optional[float]) -> str:
    if v is None:
        return "--"
    return ("+" if v >= 0 else "") + format(float(v), ".1f") + "%"


def format_daily_card(d: Dict[str, Any]) -> str:
    direction = d.get("direction", "UP")
    conf = float(d.get("confidence") or 0)
    conf_pct = int(round(conf * 100))
    conviction = d.get("conviction") or conviction_from_confidence(conf)
    action = d.get("pick_action") or conviction_from_confidence(conf)
    if conf >= 0.90:
        action = d.get("pick_action") or "SUPER BUY"
    elif conf >= 0.75:
        action = d.get("pick_action") or "BUY NOW"
    sizing = d.get("position_sizing") or {}

    news = d.get("news") or {}
    infl = int(news.get("influence_pct", 0) or 0)
    model_pct = int(news.get("model_pct", 100 - infl))
    news_summary = news.get("summary")

    rates = d.get("rates") or {}
    tr = d.get("track_record") or {}
    last5 = tr.get("last5") or []
    last5_str = " ".join(last5) if last5 else "--"

    lines = [
        "<b>GHOST PROTOCOL | WOLF Daily Card</b>",
        "Date: " + str(d.get("date", "")),
        "Model Version: " + str(d.get("model_version", "v3.2")),
        "",
        "<b>ACTION:</b> " + str(action),
        "",
        "<b>PREDICTION:</b>",
        "Direction: " + str(direction),
        "Confidence: " + str(conf_pct) + "%",
        "Conviction: " + str(conviction),
        "",
        "<b>PRICE LEVELS:</b>",
        "Current Price: " + _fmt_price(d.get("current_price")),
        "Buy Point (entry): " + _fmt_price(d.get("buy_point")),
        "Sell Target: " + _fmt_price(d.get("sell_target")),
        "Stop Loss: " + _fmt_price(d.get("stop_loss")),
        "Expected Move: " + _signed_pct(d.get("expected_move_pct")),
        "",
        "<b>1% RISK SIZING:</b>",
    ]
    if sizing.get("ok"):
        lines += [
            "Account: $" + format(float(sizing.get("account_size_usd", 0)), ",.0f")
            + " | Max loss if stop hits: $" + format(float(sizing.get("max_loss_usd", 0)), ",.2f"),
            "Suggested: " + str(sizing.get("suggested_shares", "--")) + " shares (~$"
            + format(float(sizing.get("suggested_notional_usd", 0)), ",.0f") + ")",
            "Stop distance: " + str(sizing.get("stop_distance_pct", "--")) + "%",
        ]
    else:
        lines.append("Set GHOST_ACCOUNT_SIZE for share suggestions.")
    lines += [
        "",
        "<b>NEWS INFLUENCE:</b>",
    ]
    if infl <= 0:
        lines.append("No material news in last 48hrs. Prediction is 100% model-driven.")
    else:
        if news_summary:
            lines.append(str(news_summary))
        lines.append("News influence: " + str(infl) + "% | Model logic: " + str(model_pct) + "%")

    lines += [
        "",
        "<b>RATES:</b>",
        "Prediction Rate Today: " + str(rates.get("today_pct", conf_pct)) + "%",
        "Highest Rate This Week: " + str(rates.get("week_high_pct", "--")) + "%",
        "Lowest Rate This Week: " + str(rates.get("week_low_pct", "--")) + "%",
        "",
        "<b>TRACK RECORD:</b>",
        "All-time: " + str(tr.get("wins", 0)) + "-" + str(tr.get("losses", 0))
        + " (" + str(tr.get("win_rate_pct", 0)) + "%)",
        "Last 5: " + last5_str,
        "Streak: " + str(tr.get("streak", "--")),
    ]
    return NL.join(lines)


def format_weekly_summary(d: Dict[str, Any]) -> str:
    f = d.get("followed") or {}
    at = d.get("alltime") or {}
    pnl = float(f.get("pnl_usd", 0) or 0)
    pnl_sign = "+" if pnl >= 0 else "-"
    top = d.get("top_pick") or {}
    weak = d.get("weakest_pick") or {}
    nd = d.get("news_driven") or {}

    lines = [
        "<b>GHOST PROTOCOL | WOLF Weekly Summary</b>",
        "Week of " + str(d.get("week_range", "")),
        "",
        "If you followed every pick:",
        str(f.get("wins", 0)) + "W / " + str(f.get("losses", 0)) + "L — "
        + str(f.get("win_rate_pct", 0)) + "%",
        pnl_sign + "$" + format(abs(pnl), ".2f") + " on $1,000 deployed",
        "",
        "All-time: " + str(at.get("win_rate_pct", 0)) + "% accuracy ("
        + str(at.get("wins", 0)) + "W/" + str(at.get("losses", 0)) + "L)",
        "Model retrains in: " + str(d.get("retrain_in_days", "--")) + " days",
        "Next card: Monday 8 AM CT",
        "",
        "Top Confidence Pick: " + str(top.get("day", "--")) + " @ "
        + str(top.get("confidence_pct", "--")) + "%",
        "Weakest Pick: " + str(weak.get("day", "--")) + " @ "
        + str(weak.get("confidence_pct", "--")) + "%",
        "News-Driven Picks: " + str(nd.get("count", 0)) + " of " + str(nd.get("total", 0)),
    ]
    return NL.join(lines)


def next_scan_note() -> str:
    """Human-readable scan cadence — market loop runs all day; 8 AM is the daily card only."""
    try:
        import datetime as _dt
        import pytz as _tz

        tz = _tz.timezone(os.getenv("GHOST_TZ", "America/Chicago"))
        now = _dt.datetime.now(tz)
        market_min = max(5, int(os.getenv("SCAN_INTERVAL_MARKET_MIN", "30")))
        off_min = max(5, int(os.getenv("SCAN_INTERVAL_OFFHOURS_MIN", "60")))
    except Exception:
        return "Engine scans on a schedule · daily Telegram card 8 AM CT"
    hm = now.hour * 60 + now.minute
    is_weekday = now.weekday() < 5
    from core.market_hours import is_us_premarket, is_us_rth
    from core.prediction import _premarket_scan_enabled

    if is_us_rth() or (is_us_premarket() and _premarket_scan_enabled()):
        return f"Engine scans every ~{market_min} min (market hours) · daily card 8 AM CT"
    return f"Engine scans every ~{off_min} min off-hours · daily card 8 AM CT"


_SKIP_SHORT = {
    "v3_prob_low": "prob below floor",
    "v3_regime_gate": "regime blocked",
    "v3_meta_gate": "model meta gate",
    "objective_bootstrap_conf": "conf below bootstrap",
    "objective_wr_low": "win rate below target",
    "below_confidence_floor": "conf below floor",
    "sell_blocked": "SELL blocked",
}


def format_candidate_lines(candidates: List[Dict[str, Any]]) -> List[str]:
    """Ranked near-fire leaderboard lines for the silence card."""
    lines: List[str] = []
    for i, c in enumerate(candidates or [], 1):
        prob = c.get("up_prob")
        if prob is None:
            continue
        sym = str(c.get("symbol") or "?")
        part = f"{i}. {sym} {float(prob) * 100:.1f}%"
        need = c.get("min_win_proba")
        if need is not None:
            part += f" (needs {float(need) * 100:.0f}%)"
        if c.get("fired"):
            part += " — FIRED"
        else:
            skip = c.get("skip_code")
            part += " — " + _SKIP_SHORT.get(skip, skip or "gated")
        lines.append(part)
    return lines


def format_silence_card(d: Dict[str, Any]) -> str:
    score = d.get("ghost_score", "--")
    bias = d.get("bias_label") or "composite bias"
    trade_action = d.get("trade_action") or "NO TRADE"
    trade_note = d.get("trade_note") or "No official pick — gates not cleared."
    next_scan = d.get("next_scan_note") or next_scan_note()
    lines = [
        "<b>GHOST PROTOCOL | WOLF</b>",
        "Status: SILENCE — No high-conviction signal today",
        "Ghost Score: " + str(score) + "/100 (" + str(bias) + ")",
        "Trade action: " + str(trade_action),
        "Reason: " + str(d.get("reason", "No qualifying signal")),
        trade_note,
    ]
    cand_lines = format_candidate_lines(d.get("top_candidates") or [])
    if cand_lines:
        lines += ["", "<b>Closest candidates today:</b>"] + cand_lines
    lines.append("Next scan: " + str(next_scan))
    return NL.join(lines)
