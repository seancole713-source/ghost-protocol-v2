"""Telegram card formatters — Daily Card, Weekly Summary, Silence Mode.

Pure string builders: all data is computed by the caller and passed in as plain
dicts, so the formatting is unit-testable without a DB, model, or network. The
assembly (querying picks, track record, news influence) lives in wolf_app's
scheduler jobs; this module only turns those facts into the message text.

Output uses Telegram HTML parse_mode (<b> for headers), matching core.telegram._send.
"""
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


def format_silence_card(d: Dict[str, Any]) -> str:
    lines = [
        "<b>GHOST PROTOCOL | WOLF</b>",
        "Status: SILENCE — No high-conviction signal today",
        "Ghost Score: " + str(d.get("ghost_score", "--")) + "/100",
        "Reason: " + str(d.get("reason", "No qualifying signal")),
        "Next scan: Tomorrow 8 AM CT",
    ]
    return NL.join(lines)
