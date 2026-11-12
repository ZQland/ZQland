"""Read-side queries used by the web pages and JSON API."""
from __future__ import annotations

from . import scoring
from .config import Config
from .db import connect
from .scanner import enrich

SORTS = {
    "outlier": lambda v: v["outlier_score"] or 0,
    "reach": lambda v: v["reach_ratio"] or 0,
    "views": lambda v: v["views"] or 0,
    "velocity": lambda v: v["views_per_day"] or 0,
    "engagement": lambda v: v["engagement_rate"] or 0,
    "newest": lambda v: v["published_at"] or "",
}

_VIDEO_COLS = """v.*, c.title AS channel_title, c.handle AS channel_handle, c.url AS channel_url,
    c.followers, c.median_views_short, c.median_views_long, c.sample_size, c.tracked,
    (SELECT status FROM analyses a WHERE a.video_id = v.id) AS analysis_status"""


def list_outliers(cfg: Config, genre: str | None = None, keyword: str | None = None,
                  min_outlier: float | None = None, max_subs: int | None = None,
                  sort: str = "outlier", only_qualifying: bool = True, limit: int = 200,
                  platform: str | None = None) -> list[dict]:
    where, params = [], []
    if genre:
        where.append("h.genre = ?"); params.append(genre)
    if keyword:
        where.append("h.keyword = ?"); params.append(keyword)
    if platform:
        where.append("v.platform = ?"); params.append(platform)
    sql = f"""
        SELECT {_VIDEO_COLS},
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
        rows = [r for r in rows if r["followers"] is not None and r["followers"] <= max_subs]
    rows.sort(key=SORTS.get(sort, SORTS["outlier"]), reverse=True)
    return rows[:limit]


def get_video(cfg: Config, video_id: str) -> dict | None:
    with connect(cfg.db_path) as conn:
        row = conn.execute(
            f"""SELECT {_VIDEO_COLS},
                       GROUP_CONCAT(DISTINCT h.genre) AS genres, GROUP_CONCAT(DISTINCT h.keyword) AS keywords
                FROM videos v LEFT JOIN channels c ON c.id = v.channel_id
                LEFT JOIN hits h ON h.video_id = v.id WHERE v.id = ? GROUP BY v.id""",
            (video_id,),
        ).fetchone()
    return enrich(dict(row), cfg.settings) if row and row["id"] else None


def follower_history(cfg: Config, channel_id: str) -> list[tuple[str, int]]:
    with connect(cfg.db_path) as conn:
        return [(r["taken_at"], r["followers"]) for r in conn.execute(
            "SELECT taken_at, followers FROM follower_snapshots WHERE channel_id = ? ORDER BY taken_at",
            (channel_id,))]


def followers_gained(cfg: Config, video: dict) -> dict:
    history = follower_history(cfg, video["channel_id"])
    with connect(cfg.db_path) as conn:
        window = [(r["published_at"], r["views"]) for r in conn.execute(
            "SELECT published_at, views FROM videos WHERE channel_id = ?", (video["channel_id"],))]
    est = scoring.estimate_followers_gained(history, video["published_at"], video["views"], window)
    if video["platform"] == "youtube" and est.get("estimate") is not None:
        est["note"] += " YouTube rounds public subscriber counts to 3 significant figures."
    return est


def list_creators(cfg: Config, platform: str | None = None, tracked_only: bool = True) -> list[dict]:
    where, params = [], []
    if tracked_only:
        where.append("c.tracked = 1")
    if platform:
        where.append("c.platform = ?"); params.append(platform)
    sql = f"""SELECT c.*, COUNT(v.id) AS videos_stored,
                     (SELECT COUNT(*) FROM follower_snapshots s WHERE s.channel_id = c.id) AS snapshots
              FROM channels c LEFT JOIN videos v ON v.channel_id = c.id
              {"WHERE " + " AND ".join(where) if where else ""}
              GROUP BY c.id ORDER BY c.platform, c.followers DESC"""
    with connect(cfg.db_path) as conn:
        creators = [dict(r) for r in conn.execute(sql, params)]
    for c in creators:
        posts = creator_posts(cfg, c["id"], limit=500)
        c["outliers"] = sum(1 for p in posts if (p["outlier_score"] or 0) >= cfg.settings.min_outlier_score)
        c["best_outlier"] = max((p["outlier_score"] or 0 for p in posts), default=None) or None
        hist = follower_history(cfg, c["id"])
        c["growth_30d"] = _growth(hist, 30)
    return creators


def _growth(hist: list[tuple[str, int]], days: int) -> int | None:
    if len(hist) < 2:
        return None
    end_t, end_f = hist[-1]
    cutoff = scoring.parse_time(end_t).timestamp() - days * 86400
    start = next(((t, f) for t, f in hist if scoring.parse_time(t).timestamp() >= cutoff), None)
    return end_f - start[1] if start and start[0] != end_t else None


def get_creator(cfg: Config, channel_id: str) -> dict | None:
    with connect(cfg.db_path) as conn:
        row = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    return dict(row) if row else None


def creator_posts(cfg: Config, channel_id: str, sort: str = "outlier", limit: int = 100) -> list[dict]:
    with connect(cfg.db_path) as conn:
        rows = [enrich(dict(r), cfg.settings) for r in conn.execute(
            f"SELECT {_VIDEO_COLS} FROM videos v LEFT JOIN channels c ON c.id = v.channel_id "
            "WHERE v.channel_id = ?", (channel_id,))]
    rows.sort(key=SORTS.get(sort, SORTS["outlier"]), reverse=True)
    return rows[:limit]


def keyword_opportunities(cfg: Config, genre: str | None = None, platform: str | None = None) -> list[dict]:
    where, params = [], []
    if genre:
        where.append("genre = ?"); params.append(genre)
    if platform:
        where.append("platform = ?"); params.append(platform)
    with connect(cfg.db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM keyword_stats" + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY opportunity DESC", params).fetchall()
    return [dict(r) for r in rows]


def recent_scans(cfg: Config, limit: int = 20) -> list[dict]:
    with connect(cfg.db_path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT ?", (limit,))]


def summary(cfg: Config) -> dict:
    with connect(cfg.db_path) as conn:
        videos = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        channels = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        tracked = conn.execute("SELECT COUNT(*) FROM channels WHERE tracked = 1").fetchone()[0]
    return {"videos": videos, "channels": channels, "tracked": tracked}
