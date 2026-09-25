"""The only judges: intervals, expectancy, calibration, sample size.

A win rate without its interval is an anecdote. These functions are the
vocabulary every edge report must use -- no point estimate is ever shown alone.
"""
from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


def wilson(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """Wilson score interval for k successes in n trials."""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def diff_ci(k1: int, n1: int, k2: int, n2: int, z: float = 1.96) -> Optional[Tuple[float, float, float]]:
    """(p1 - p2, low, high): Newcombe's hybrid score interval for a difference of two rates.

    Built from the two Wilson intervals, so it behaves at small n and near 0 or 1 where the
    plain normal-approximation interval does not. None when either side has no trials.
    """
    if n1 <= 0 or n2 <= 0:
        return None
    p1, p2 = k1 / n1, k2 / n2
    l1, u1 = wilson(k1, n1, z)
    l2, u2 = wilson(k2, n2, z)
    d = p1 - p2
    lo = d - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    hi = d + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    return d, max(-1.0, lo), min(1.0, hi)


def expectancy(pnls: Sequence[float]) -> Dict[str, Optional[float]]:
    """Average result per trade, and the pieces it is made of."""
    xs = [float(x) for x in pnls]
    wins = [x for x in xs if x > 0]
    losses = [x for x in xs if x < 0]
    n = len(xs)
    return {
        "n": n,
        "mean": sum(xs) / n if n else None,
        "avg_win": sum(wins) / len(wins) if wins else None,
        "avg_loss": sum(losses) / len(losses) if losses else None,
        "total": sum(xs),
        "profit_factor": (sum(wins) / -sum(losses)) if losses and wins else None,
    }


def trades_needed(p_true: float, p_null: float, *, z_alpha: float = 1.645,
                  z_beta: float = 0.84) -> int:
    """Trades needed to show p_true beats p_null (one-sided, ~95% / 80% power).

    The honest answer to "when will we know?" -- e.g. 45% vs a 37.5%
    break-even needs a few hundred trades, not twenty.
    """
    if not (0 < p_null < p_true < 1):
        raise ValueError("need 0 < p_null < p_true < 1")
    a = z_alpha * math.sqrt(p_null * (1 - p_null))
    b = z_beta * math.sqrt(p_true * (1 - p_true))
    return math.ceil(((a + b) / (p_true - p_null)) ** 2)


def calibration_bands(
    probs: Sequence[float], hits: Sequence[bool],
    edges: Sequence[float] = (0.0, 0.5, 0.55, 0.6, 0.65, 0.7, 0.8, 1.0001),
    min_n: int = 20,
) -> List[Dict[str, object]]:
    """Stated probability vs realised frequency, per band, with intervals."""
    out = []
    for lo, hi in zip(edges, edges[1:]):
        idx = [i for i, p in enumerate(probs) if lo <= p < hi]
        n = len(idx)
        k = sum(1 for i in idx if hits[i])
        ci = wilson(k, n) if n else (None, None)
        out.append({
            "band": f"{lo:.0%}-{min(hi, 1.0):.0%}", "n": n, "hits": k,
            "stated_mean": (sum(probs[i] for i in idx) / n) if n else None,
            "realised": k / n if n else None, "ci_low": ci[0], "ci_high": ci[1],
            "thin": n < min_n,
        })
    return out


def is_monotonic(bands: Sequence[Dict[str, object]]) -> Optional[bool]:
    """True when realised rates rise with stated probability across non-thin bands."""
    rates = [b["realised"] for b in bands if not b["thin"] and b["realised"] is not None]
    if len(rates) < 2:
        return None
    return all(b >= a - 1e-12 for a, b in zip(rates, rates[1:]))


def isotonic_fit(xs: Sequence[float], ys: Sequence[float]) -> List[Tuple[float, float]]:
    """Pool-adjacent-violators: a monotone map from score to frequency.

    Returns (x_upper_bound, calibrated_value) steps. Fit on a calibration split
    only -- never on the data it will be judged on.
    """
    pts = sorted(zip(xs, ys))
    blocks: List[List[float]] = []   # [sum_y, count, max_x]
    for x, y in pts:
        blocks.append([float(y), 1.0, float(x)])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            s, c, mx = blocks.pop()
            blocks[-1][0] += s; blocks[-1][1] += c; blocks[-1][2] = mx
    return [(mx, s / c) for s, c, mx in blocks]


def isotonic_apply(steps: Sequence[Tuple[float, float]], x: float) -> Optional[float]:
    if not steps:
        return None
    for upper, value in steps:
        if x <= upper:
            return value
    return steps[-1][1]


def walk_forward_folds(n: int, *, train: int, test: int, step: Optional[int] = None):
    """Time-ordered (train_idx, test_idx) folds. Never shuffled -- shuffling
    lets the future leak into the past and invents skill."""
    step = step or test
    start = 0
    while start + train + test <= n:
        yield (range(start, start + train), range(start + train, start + train + test))
        start += step


def clustered_bootstrap_ci(results_by_day: Dict[str, Sequence[bool]], *, iters: int = 4000,
                           seed: int = 7, alpha: float = 0.05) -> Tuple[Optional[float], Optional[float]]:
    """Win-rate interval that resamples whole SESSIONS, not trades.

    Trades on one day share a market; treating them as independent overstates
    the evidence. When days are few, this interval is honestly wide.
    """
    import random
    days = [list(v) for v in results_by_day.values() if len(v)]
    if not days:
        return None, None
    rng = random.Random(seed)
    rates = []
    for _ in range(iters):
        sample = [days[rng.randrange(len(days))] for _ in days]
        n = sum(len(d) for d in sample)
        rates.append(sum(sum(d) for d in sample) / n)
    rates.sort()
    lo = rates[int(alpha / 2 * iters)]
    hi = rates[min(iters - 1, int((1 - alpha / 2) * iters))]
    return lo, hi


def bonferroni_alpha(alpha: float, n_candidates: int) -> float:
    """Testing k strategies at once: each must clear alpha / k."""
    return alpha / max(1, n_candidates)


MIN_FILLED = 30     # below this many filled trades an interval is reported, never judged


def break_even_verdict(wins: int, n: int, break_even: float, *, style: str = "backtest",
                       none: str = "no filled trades") -> str:
    """The one verdict every edge report uses: the Wilson interval against break-even.

    Below MIN_FILLED filled trades no interval is "evidence" in either direction --
    three wins in three trades has a Wilson range that clears almost any break-even
    and says nothing. The range is still shown so the reader sees how wide it is.

    style "ledger" keeps the forward ledger's wording ("edge shown: ..."), style
    "backtest" the backtests' ("above break-even across the whole interval").
    """
    if n <= 0:
        return none
    lo, hi = wilson(wins, n)
    if n < MIN_FILLED:
        return f"too few trades (n={n}): Wilson range {lo:.0%}-{hi:.0%} is not evidence"
    if style == "ledger":
        if lo > break_even:
            return "edge shown: the whole interval is above break-even"
        if hi < break_even:
            return "no edge: the whole interval is below break-even"
        return f"undecided: the interval straddles break-even ({break_even:.1%})"
    if lo > break_even:
        return "above break-even across the whole interval"
    if hi < break_even:
        return "below break-even across the whole interval"
    return "undecided: the interval straddles break-even"
