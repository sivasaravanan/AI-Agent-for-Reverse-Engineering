"""Trends over time from the history database: finding counts per run of the same project."""
from __future__ import annotations

from typing import Any, Dict

from database.db_manager import DbManager


def project_trend(db: DbManager, archive_name: str, limit: int = 50) -> Dict[str, Any]:
    rows = [r for r in db.list_history(limit=500) if r.archive_name == archive_name][:limit]
    rows.sort(key=lambda r: r.id)
    points = [{"id": r.id, "at": r.analyzed_at, "total": r.finding_count, "high": r.high_count}
              for r in rows]
    direction, delta = "insufficient data", 0
    if len(points) >= 2:
        delta = points[-1]["high"] - points[0]["high"]
        last = points[-1]["total"] - points[-2]["total"]
        direction = "improving" if (delta < 0 or (delta == 0 and last < 0)) else "worsening" if (delta > 0 or last > 0) else "unchanged"
    return {"project": archive_name, "runs": len(points), "points": points, "direction": direction, "high_delta_since_first": delta,
            "note": "Counts compare stored runs; a rule or analyzer change between runs also changes counts."}


def portfolio_trends(db: DbManager) -> Dict[str, Any]:
    names = sorted({r.archive_name for r in db.list_history(limit=500)})
    return {n: project_trend(db, n) for n in names}
