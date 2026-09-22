"""Position sizing and the stops that protect the account, not the trade.

The per-trade stop and the daily limit must agree. Ghost's shipped settings had
a $2,500 per-trade stop beside a $250 daily limit -- one trade could blow the
day tenfold -- because the two were configured independently. Here they are
derived together and checked against each other.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class RiskPolicy:
    account_usd: float
    max_position_usd: float          # notional cap per trade
    max_risk_per_trade_pct: float    # of account, entry-to-stop
    max_daily_loss_pct: float        # realised, then no new trades today
    max_trades_per_day: int
    pause_line_usd: float            # cumulative experiment loss that stops it

    def problems(self) -> List[str]:
        out = []
        per_trade = self.account_usd * self.max_risk_per_trade_pct / 100
        daily = self.account_usd * self.max_daily_loss_pct / 100
        if per_trade > daily:
            out.append(f"one stopped trade (${per_trade:,.0f}) exceeds the daily limit (${daily:,.0f})")
        if self.max_position_usd > self.account_usd:
            out.append("position cap exceeds the account")
        if self.pause_line_usd >= 0:
            out.append("pause line must be a loss (negative)")
        return out


@dataclass(frozen=True)
class SizeDecision:
    allowed: bool
    shares: int
    notional_usd: float
    risk_usd: float
    reasons: tuple


def size(policy: RiskPolicy, *, entry: float, stop: float, trades_today: int,
         realised_today_usd: float, experiment_pnl_usd: float) -> SizeDecision:
    reasons = []
    if policy.problems():
        reasons.extend(policy.problems())
    if trades_today >= policy.max_trades_per_day:
        reasons.append("daily trade count reached")
    if realised_today_usd <= -policy.account_usd * policy.max_daily_loss_pct / 100:
        reasons.append("daily loss limit reached")
    if experiment_pnl_usd <= policy.pause_line_usd:
        reasons.append("experiment pause line reached -- review before trading again")
    if not (0 < stop < entry):
        reasons.append("stop must be below entry")
    if reasons:
        return SizeDecision(False, 0, 0.0, 0.0, tuple(reasons))
    per_share_risk = entry - stop
    by_notional = int(policy.max_position_usd // entry)
    by_risk = int((policy.account_usd * policy.max_risk_per_trade_pct / 100) // per_share_risk)
    shares = max(0, min(by_notional, by_risk))
    if shares < 1:
        return SizeDecision(False, 0, 0.0, 0.0, ("size rounds to zero shares",))
    return SizeDecision(True, shares, round(shares * entry, 2),
                        round(shares * per_share_risk, 2), ())


# The operator's real account, sized for Gap-and-Go v1 (Option A).
OPERATOR_V1 = RiskPolicy(
    account_usd=10_000, max_position_usd=1_000, max_risk_per_trade_pct=0.5,
    max_daily_loss_pct=1.0, max_trades_per_day=2, pause_line_usd=-300,
)


@dataclass(frozen=True)
class OpenRisk:
    symbol: str
    risk_usd: float          # entry-to-stop, at STRESSED execution
    theme: str               # the catalyst family driving it


def portfolio_check(policy: RiskPolicy, open_positions: List[OpenRisk], candidate: OpenRisk, *,
                    max_open_risk_pct: float = 1.0, max_theme_risk_pct: float = 0.5) -> List[str]:
    """Problems with adding `candidate` to what is already open.

    Three stocks moving on one catalyst are one bet, not three -- so risk is
    capped per THEME as well as in total. Risk is measured at stressed
    execution (a stop that fills worse than its price), not the stop level.
    """
    out = []
    acct = policy.account_usd
    total = sum(p.risk_usd for p in open_positions) + candidate.risk_usd
    if total > acct * max_open_risk_pct / 100:
        out.append(f"total open risk ${total:,.0f} exceeds {max_open_risk_pct:g}% of the account")
    theme = sum(p.risk_usd for p in open_positions if p.theme == candidate.theme) + candidate.risk_usd
    if theme > acct * max_theme_risk_pct / 100:
        out.append(f"theme '{candidate.theme}' risk ${theme:,.0f} exceeds {max_theme_risk_pct:g}% -- correlated positions")
    if any(p.symbol == candidate.symbol for p in open_positions):
        out.append(f"{candidate.symbol} is already open")
    return out


def stressed_risk_usd(shares: int, entry: float, stop: float, *, stop_slip_pct: float = 1.0) -> float:
    """A triggered stop becomes a market order; assume it fills worse than the stop."""
    return round(shares * (entry - stop * (1 - stop_slip_pct / 100)), 2)
