"""Read-side queries used by the web pages and JSON API."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    c.followers, c.median_views_short, c.median_views_long, c.sample_size, c.tracked, c.genre AS channel_genre,
    (SELECT status FROM analyses a WHERE a.video_id = v.id) AS analysis_status"""


Filter = str | list[str] | tuple[str, ...] | None


def _any_of(where: list[str], params: list, column: str, value: Filter) -> None:
    """Add `column IN (...)` for a filter given as one value or several; empty means no filter."""
    values = [v for v in ([value] if isinstance(value, str) else (value or [])) if v]
    if values:
        where.append(f"{column} IN ({','.join('?' * len(values))})")
        params.extend(values)


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def list_outliers(cfg: Config, genre: Filter = None, keyword: Filter = None,
                  min_outlier: float | None = None, max_subs: int | None = None,
                  sort: str = "outlier", only_qualifying: bool = True, limit: int = 200,
                  platform: Filter = None) -> list[dict]:
    """Outliers from keyword results AND from creators' own posts (creator list, lookups).
    A genre matches a keyword hit's genre or the creator's genre; keywords match hits only."""
    where, params = ["v.published_at >= ?"], [_since(cfg.settings.lookback_days)]
    genres = [genre] if isinstance(genre, str) else [g for g in (genre or []) if g]
    if genres:
        marks = ",".join("?" * len(genres))
        where.append(f"(c.genre IN ({marks}) OR EXISTS (SELECT 1 FROM hits g WHERE g.video_id = v.id "
                     f"AND g.genre IN ({marks})))")
        params += genres + genres
    kws = [keyword] if isinstance(keyword, str) else [k for k in (keyword or []) if k]
    if kws:
        where.append(f"EXISTS (SELECT 1 FROM hits k WHERE k.video_id = v.id AND k.keyword IN ({','.join('?' * len(kws))}))")
        params += kws
    _any_of(where, params, "v.platform", platform)
    sql = f"""
        SELECT {_VIDEO_COLS},
               GROUP_CONCAT(DISTINCT h.genre) AS genres, GROUP_CONCAT(DISTINCT h.keyword) AS keywords
        FROM videos v LEFT JOIN hits h ON h.video_id = v.id LEFT JOIN channels c ON c.id = v.channel_id
        WHERE {" AND ".join(where)}
        GROUP BY v.id"""
    with connect(cfg.db_path) as conn:
        rows = [enrich(dict(r), cfg.settings) for r in conn.execute(sql, params)]
    for r in rows:
        r["source"] = "keyword" if r["keywords"] else "creator"
    if only_qualifying:
        rows = [r for r in rows if r["qualifies"]]
    _attach_verdicts(cfg, rows)
    if min_outlier:
        rows = [r for r in rows if (r["outlier_score"] or 0) >= min_outlier]
    if max_subs:
        rows = [r for r in rows if r["followers"] is not None and r["followers"] <= max_subs]
    rows.sort(key=SORTS.get(sort, SORTS["outlier"]), reverse=True)
    rows = rows[:limit]
    _attach_verdicts(cfg, rows)
    return rows


def suggested_creators(cfg: Config, platform: Filter = None, genre: Filter = None, limit: int = 60) -> list[dict]:
    """Untracked creators already in the database with at least one qualifying outlier: free to add,
    since their posts and medians are stored. Best candidates for the creator list."""
    best: dict[str, dict] = {}
    for v in list_outliers(cfg, genre=genre, platform=platform, limit=100_000):
        if v["tracked"]:
            continue
        b = best.setdefault(v["channel_id"], {
            "id": v["channel_id"], "platform": v["platform"], "title": v["channel_title"],
            "handle": v["channel_handle"], "followers": v["followers"], "genre": v["channel_genre"],
            "median": v["channel_median"], "outliers": 0, "best": 0.0})
        b["outliers"] += 1
        b["best"] = max(b["best"], v["outlier_score"] or 0)
    return sorted(best.values(), key=lambda c: (c["outliers"], c["best"]), reverse=True)[:limit]


def tracked_counts(cfg: Config) -> dict:
    """Tracked creators per genre ("" = no genre) and per platform, for the Scans page."""
    with connect(cfg.db_path) as conn:
        rows = conn.execute("SELECT COALESCE(genre, '') AS genre, platform, COUNT(*) AS n FROM channels "
                            "WHERE tracked = 1 GROUP BY 1, 2").fetchall()
    by_genre: dict[str, int] = {}
    by_platform: dict[str, int] = {}
    for r in rows:
        by_genre[r["genre"]] = by_genre.get(r["genre"], 0) + r["n"]
        by_platform[r["platform"]] = by_platform.get(r["platform"], 0) + r["n"]
    return {"genre": by_genre, "platform": by_platform, "total": sum(by_genre.values())}


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


def list_creators(cfg: Config, platform: Filter = None, tracked_only: bool = True,
                  genre: Filter = None) -> list[dict]:
    where, params = [], []
    if tracked_only:
        where.append("c.tracked = 1")
    _any_of(where, params, "c.platform", platform)
    _any_of(where, params, "c.genre", genre)
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
    rows = rows[:limit]
    _attach_verdicts(cfg, rows)
    return rows


def keyword_opportunities(cfg: Config, genre: Filter = None, platform: Filter = None) -> list[dict]:
    where, params = [], []
    _any_of(where, params, "genre", genre)
    _any_of(where, params, "platform", platform)
    with connect(cfg.db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM keyword_stats" + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY opportunity DESC", params).fetchall()
    return [dict(r) for r in rows]


def recent_problems(cfg: Config, hours: int = 24) -> list[dict]:
    """Background jobs that failed or only partly worked recently, newest first, for the site-wide
    banner. Dismissing the banner hides everything up to that moment."""
    with connect(cfg.db_path) as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key = 'alerts_dismissed_at'").fetchone()
        since = max(_since(hours / 24), row["value"] if row else "")
        scans = conn.execute(
            "SELECT id, target, status, message, finished_at FROM scans WHERE status IN ('failed', 'partial') "
            "AND finished_at > ? ORDER BY finished_at DESC LIMIT 5", (since,)).fetchall()
        analyses = conn.execute(
            "SELECT a.video_id, a.error, a.updated_at, v.title FROM analyses a LEFT JOIN videos v ON v.id = a.video_id "
            "WHERE a.status = 'failed' AND a.updated_at > ? ORDER BY a.updated_at DESC LIMIT 5", (since,)).fetchall()
    out = [{"at": r["finished_at"], "title": f"Scan #{r['id']} {'failed' if r['status'] == 'failed' else 'partly failed'}"
                                              f" ({r['target']})",
            "message": r["message"] or "", "link": "/scans"} for r in scans]
    out += [{"at": r["updated_at"], "title": f"Analysis failed: {(r['title'] or r['video_id'])[:60]}",
             "message": r["error"] or "", "link": f"/video/{r['video_id']}"} for r in analyses]
    return sorted(out, key=lambda a: a["at"], reverse=True)


def dismiss_problems(cfg: Config) -> None:
    with connect(cfg.db_path) as conn:
        conn.execute("INSERT INTO app_state (key, value) VALUES ('alerts_dismissed_at', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (_since(0),))


def recent_scans(cfg: Config, limit: int = 20) -> list[dict]:
    with connect(cfg.db_path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT ?", (limit,))]


def summary(cfg: Config) -> dict:
    with connect(cfg.db_path) as conn:
        videos = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        channels = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        tracked = conn.execute("SELECT COUNT(*) FROM channels WHERE tracked = 1").fetchone()[0]
    return {"videos": videos, "channels": channels, "tracked": tracked}


def _attach_verdicts(cfg: Config, rows: list[dict]) -> None:
    """Give analyzed videos their overall score and works / doesn't-work counts for the cards."""
    import json
    from .verdict import verdict
    ids = [r["id"] for r in rows if r.get("analysis_status") == "done"]
    if not ids:
        return
    with connect(cfg.db_path) as conn:
        found = {vid: raw for vid, raw in conn.execute(
            f"SELECT video_id, breakdown_json FROM analyses WHERE status = 'done' AND video_id IN ({','.join('?' * len(ids))})",
            ids)}
    for r in rows:
        raw = found.get(r["id"])
        if raw:
            b = json.loads(raw)
            vd = verdict(b)
            r["overall"] = b.get("overall_score")
            r["n_works"], r["n_fails"] = len(vd["works"]), len(vd["fails"])


def list_breakdowns(cfg: Config, sort: str = "newest") -> list[dict]:
    """Every finished breakdown with its verdict, for the Breakdowns page."""
    import json
    from .verdict import verdict
    sql = f"""SELECT {_VIDEO_COLS}, a.breakdown_json, a.updated_at AS analyzed_at, a.demo AS analysis_demo
              FROM analyses a JOIN videos v ON v.id = a.video_id LEFT JOIN channels c ON c.id = v.channel_id
              WHERE a.status = 'done'"""
    with connect(cfg.db_path) as conn:
        rows = [dict(r) for r in conn.execute(sql)]
    out = []
    for r in rows:
        b = json.loads(r.pop("breakdown_json") or "{}")
        r = enrich(r, cfg.settings)
        r["b"], r["verdict"], r["overall"] = b, verdict(b), b.get("overall_score")
        out.append(r)
    keys = {"score": lambda r: r["overall"] or 0, "outlier": lambda r: r["outlier_score"] or 0,
            "newest": lambda r: r["analyzed_at"] or ""}
    out.sort(key=keys.get(sort, keys["newest"]), reverse=True)
    return out
