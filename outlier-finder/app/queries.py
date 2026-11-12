"""Read-side queries used by the web pages and JSON API."""
from __future__ import annotations

from .config import Config
from .db import connect
from .scanner import enrich

SORTS = {
    "outlier": lambda v: v["outlier_score"] or 0,
    "reach": lambda v: v["reach_ratio"] or 0,
    "views": lambda v: v["views"],
    "velocity": lambda v: v["views_per_day"],
    "newest": lambda v: v["published_at"] or "",
}


def list_outliers(cfg: Config, genre: str | None = None, keyword: str | None = None,
                  min_outlier: float | None = None, max_subs: int | None = None,
                  sort: str = "outlier", only_qualifying: bool = True, limit: int = 200) -> list[dict]:
    where, params = [], []
    if genre:
        where.append("h.genre = ?"); params.append(genre)
    if keyword:
        where.append("h.keyword = ?"); params.append(keyword)
    sql = f"""
        SELECT v.*, c.title AS channel_title, c.subscribers, c.median_views_short,
               c.median_views_long, c.sample_size,
               GROUP_CONCAT(DISTINCT h.genre) AS genres, GROUP_CONCAT(DISTINCT h.keyword) AS keywords
        FROM videos v JOIN hits h ON h.video_id = v.id LEFT JOIN channels c ON c.id = v.channel_id
        {"WHERE " + " AND ".join(where) if where else ""}
        GROUP BY v.id"""
    with connect(cfg.db_path) as conn:
        rows = [enrich(dict(r), cfg.settings) for r in conn.execute(sql, params)]
    if only_qualifying:
        rows = [r for r in rows if r["qualifies"]]
    if min_outlier:
        rows = [r for r in rows if (r["outlier_score"] or 0) >= min_outlier]
    if max_subs:
        rows = [r for r in rows if r["subscribers"] is not None and r["subscribers"] <= max_subs]
    rows.sort(key=SORTS.get(sort, SORTS["outlier"]), reverse=True)
    return rows[:limit]


def get_video(cfg: Config, video_id: str) -> dict | None:
    with connect(cfg.db_path) as conn:
        row = conn.execute(
            """SELECT v.*, c.title AS channel_title, c.subscribers, c.median_views_short,
                      c.median_views_long, c.sample_size,
                      GROUP_CONCAT(DISTINCT h.genre) AS genres, GROUP_CONCAT(DISTINCT h.keyword) AS keywords
               FROM videos v LEFT JOIN channels c ON c.id = v.channel_id
               LEFT JOIN hits h ON h.video_id = v.id WHERE v.id = ? GROUP BY v.id""",
            (video_id,),
        ).fetchone()
    return enrich(dict(row), cfg.settings) if row and row["id"] else None


def keyword_opportunities(cfg: Config, genre: str | None = None) -> list[dict]:
    with connect(cfg.db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM keyword_stats" + (" WHERE genre = ?" if genre else "")
            + " ORDER BY opportunity DESC", ([genre] if genre else []),
        ).fetchall()
    return [dict(r) for r in rows]


def recent_scans(cfg: Config, limit: int = 20) -> list[dict]:
    with connect(cfg.db_path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM scans ORDER BY id DESC LIMIT ?", (limit,))]


def summary(cfg: Config) -> dict:
    with connect(cfg.db_path) as conn:
        videos = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        channels = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        last = conn.execute("SELECT MAX(scanned_at) FROM keyword_stats").fetchone()[0]
    return {"videos": videos, "channels": channels, "last_scan": last}
