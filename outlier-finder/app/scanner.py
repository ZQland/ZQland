"""Runs keyword scans: search → video stats → channel medians → store → score."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import scoring
from .config import Config
from .db import connect
from .youtube import QuotaExceeded, YouTubeClient

log = logging.getLogger(__name__)

MIN_AGE_FOR_MEDIAN_DAYS = 2  # very fresh uploads are still growing; skip them


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _video_row(item: dict) -> dict:
    sn, st = item.get("snippet", {}), item.get("statistics", {})
    thumbs = sn.get("thumbnails", {})
    thumb = (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url")
    return {
        "id": item["id"],
        "channel_id": sn.get("channelId"),
        "title": sn.get("title", ""),
        "description": (sn.get("description") or "")[:2000],
        "published_at": sn.get("publishedAt"),
        "duration_s": scoring.parse_duration(item.get("contentDetails", {}).get("duration")),
        "views": int(st.get("viewCount", 0)),
        "likes": int(st["likeCount"]) if "likeCount" in st else None,
        "comments": int(st["commentCount"]) if "commentCount" in st else None,
        "thumbnail": thumb,
    }


def _channels_needing_refresh(conn, channel_ids: list[str], cache_days: int) -> list[str]:
    cutoff = _iso(_now() - timedelta(days=cache_days))
    fresh = {
        r["id"] for r in conn.execute(
            f"SELECT id FROM channels WHERE updated_at >= ? AND id IN ({','.join('?' * len(channel_ids))})",
            [cutoff, *channel_ids],
        )
    } if channel_ids else set()
    return [c for c in channel_ids if c not in fresh]


def _refresh_channels(conn, client: YouTubeClient, cfg: Config, channel_ids: list[str]) -> None:
    s = cfg.settings
    for item in client.channels(channel_ids):
        st = item.get("statistics", {})
        hidden = st.get("hiddenSubscriberCount", False)
        subs = None if hidden else int(st.get("subscriberCount", 0))
        uploads = item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        med_short = med_long = None
        sample = 0
        # Only spend quota on medians for channels in the size range we care about.
        in_range = subs is not None and s.min_subscribers <= subs <= s.max_subscribers
        if in_range and uploads:
            ids = client.recent_upload_ids(uploads, s.channel_sample_size)
            recent = [_video_row(v) for v in client.videos(ids)]
            settled = [v for v in recent
                       if v["published_at"] and scoring.age_days(v["published_at"]) >= MIN_AGE_FOR_MEDIAN_DAYS]
            med_short = scoring.channel_median([v["views"] for v in settled if scoring.is_short(v["duration_s"])])
            med_long = scoring.channel_median([v["views"] for v in settled if not scoring.is_short(v["duration_s"])])
            sample = len(settled)
        conn.execute(
            """INSERT INTO channels (id, title, subscribers, uploads_playlist,
                   median_views_short, median_views_long, sample_size, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET title=excluded.title, subscribers=excluded.subscribers,
                   uploads_playlist=excluded.uploads_playlist, median_views_short=excluded.median_views_short,
                   median_views_long=excluded.median_views_long, sample_size=excluded.sample_size,
                   updated_at=excluded.updated_at""",
            (item["id"], item.get("snippet", {}).get("title"), subs, uploads,
             med_short, med_long, sample, _iso(_now())),
        )


def save_video(conn, v: dict, genre: str, keyword: str) -> None:
    conn.execute(
        """INSERT INTO videos (id, channel_id, title, description, published_at, duration_s,
               views, likes, comments, thumbnail, fetched_at)
           VALUES (:id, :channel_id, :title, :description, :published_at, :duration_s,
               :views, :likes, :comments, :thumbnail, :fetched_at)
           ON CONFLICT(id) DO UPDATE SET views=excluded.views, likes=excluded.likes,
               comments=excluded.comments, title=excluded.title, fetched_at=excluded.fetched_at""",
        {**v, "fetched_at": _iso(_now())},
    )
    conn.execute("INSERT OR IGNORE INTO hits (video_id, genre, keyword) VALUES (?, ?, ?)",
                 (v["id"], genre, keyword))


def enrich(row: dict, settings) -> dict:
    """Add computed metrics to a joined video+channel row."""
    short = scoring.is_short(row["duration_s"] or 0)
    med = row.get("median_views_short") if short else row.get("median_views_long")
    score = scoring.outlier_score(row["views"], med)
    out = dict(row)
    out.update(
        is_short=short,
        channel_median=med,
        outlier_score=round(score, 1) if score else None,
        tier=scoring.outlier_tier(score),
        reach_ratio=round(r, 1) if (r := scoring.reach_ratio(row["views"], row.get("subscribers"))) else None,
        views_per_day=round(scoring.views_per_day(row["views"], row["published_at"])),
        qualifies=bool(
            score is not None
            and score >= settings.min_outlier_score
            and row["views"] >= settings.min_views
            and row.get("subscribers") is not None
            and settings.min_subscribers <= row["subscribers"] <= settings.max_subscribers
            and (short or not settings.shorts_only)
        ),
    )
    return out


def update_keyword_stats(conn, cfg: Config, genre: str, keyword: str, supply: int | None) -> dict:
    rows = conn.execute(
        """SELECT v.*, c.subscribers, c.median_views_short, c.median_views_long, c.title AS channel_title
           FROM hits h JOIN videos v ON v.id = h.video_id LEFT JOIN channels c ON c.id = v.channel_id
           WHERE h.genre = ? AND h.keyword = ?""",
        (genre, keyword),
    ).fetchall()
    enriched = [enrich(dict(r), cfg.settings) for r in rows]
    if cfg.settings.shorts_only:
        enriched = [e for e in enriched if e["is_short"]]
    outliers = [e["outlier_score"] for e in enriched if e["qualifies"]]
    stats = {
        "genre": genre, "keyword": keyword, "scanned_at": _iso(_now()), "supply": supply,
        "candidates": len(enriched), "outliers": len(outliers),
        "median_outlier": round(sorted(outliers)[len(outliers) // 2], 1) if outliers else None,
        "opportunity": scoring.opportunity_score(outliers, supply),
    }
    conn.execute(
        """INSERT INTO keyword_stats VALUES (:genre, :keyword, :scanned_at, :supply, :candidates,
               :outliers, :median_outlier, :opportunity)
           ON CONFLICT(genre, keyword) DO UPDATE SET scanned_at=excluded.scanned_at,
               supply=excluded.supply, candidates=excluded.candidates, outliers=excluded.outliers,
               median_outlier=excluded.median_outlier, opportunity=excluded.opportunity""",
        stats,
    )
    return stats


def scan_keyword(conn, client: YouTubeClient, cfg: Config, genre: str, keyword: str) -> dict:
    s = cfg.settings
    ids, supply = client.search(keyword, s.lookback_days, s.results_per_keyword, s.shorts_only)
    videos = [_video_row(v) for v in client.videos(ids)] if ids else []
    if s.shorts_only:
        videos = [v for v in videos if scoring.is_short(v["duration_s"])]
    channel_ids = sorted({v["channel_id"] for v in videos if v["channel_id"]})
    stale = _channels_needing_refresh(conn, channel_ids, s.channel_cache_days)
    if stale:
        _refresh_channels(conn, client, cfg, stale)
    for v in videos:
        save_video(conn, v, genre, keyword)
    return update_keyword_stats(conn, cfg, genre, keyword, supply)


def start_scan(cfg: Config, target: str) -> int:
    with connect(cfg.db_path) as conn:
        cur = conn.execute("INSERT INTO scans (target, started_at, status) VALUES (?, ?, 'running')",
                           (target, _iso(_now())))
        return cur.lastrowid


def run_scan(cfg: Config, scan_id: int, genres: list[str], client: YouTubeClient | None = None) -> None:
    """Scan every keyword in the given genres. Safe to run in a background thread."""
    client = client or YouTubeClient(cfg.api_key)
    status, message, done = "finished", "", 0
    keywords = [(g, k) for g in genres for k in cfg.genres.get(g, [])]
    try:
        for genre, keyword in keywords:
            with connect(cfg.db_path) as conn:
                stats = scan_keyword(conn, client, cfg, genre, keyword)
                done += 1
                conn.execute("UPDATE scans SET quota_used=?, message=? WHERE id=?",
                             (client.quota_used, f"{done}/{len(keywords)} keywords · last: {keyword} "
                              f"({stats['outliers']} outliers)", scan_id))
    except QuotaExceeded as e:
        status, message = "quota_exceeded", f"{e} Completed {done}/{len(keywords)} keywords."
    except Exception as e:  # surface any failure in the UI instead of dying silently
        log.exception("scan failed")
        status, message = "failed", f"{type(e).__name__}: {e} (after {done}/{len(keywords)} keywords)"
    else:
        message = f"Scanned {done} keywords."
    with connect(cfg.db_path) as conn:
        conn.execute("UPDATE scans SET finished_at=?, status=?, quota_used=?, message=? WHERE id=?",
                     (_iso(_now()), status, client.quota_used, message, scan_id))
