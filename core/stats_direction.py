"""Shared direction breakdown for /api/stats/direction and /api/cockpit/context."""


def compute_stats_by_direction(cur):
    """Run direction breakdown using an existing DB cursor."""
    cur.execute(
        """
        SELECT direction,
               COUNT(*) as total,
               SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins,
               ROUND(AVG(CASE WHEN pnl_pct IS NOT NULL THEN pnl_pct ELSE 0 END)::numeric,2) as avg_pnl
        FROM predictions
        WHERE outcome IN ('WIN','LOSS','STOP','EXPIRED') AND id >= 223438
        GROUP BY direction
        """
    )
    rows = cur.fetchall()
    result = {}
    for r in rows:
        d = "BUY" if r[0] in ("UP", "BUY") else "SELL"
        total = int(r[1])
        wins = int(r[2])
        result[d] = {
            "total": total,
            "wins": wins,
            "losses": total - wins,
            "win_rate_pct": round(wins / total * 100, 1) if total else 0,
            "avg_pnl": float(r[3]),
        }
    return {"ok": True, "by_direction": result}
