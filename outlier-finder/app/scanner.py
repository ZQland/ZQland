"""Keyword scans, creator tracking and follower snapshots across all platforms."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import scoring
from .config import Config
from .db import connect
from .models import Post, Profile
from .providers import source_for
from .providers.base import ProviderUnavailable
from .providers.youtube import QuotaExceeded

log = logging.getLogger(__name__)

MIN_AGE_FOR_MEDIAN_DAYS = 2   # very fresh uploads are still growing; skip them
SNAPSHOT_MIN_GAP_HOURS = 6    # don't store follower snapshots more often than this


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# --- writes -------------------------------------------------------------------

def save_post(conn, p: Post, genre: str | None = None, keyword: str | None = None) -> None:
    conn.execute(
        """INSERT INTO videos (id, platform, channel_id, title, description, published_at, duration_s,
               views, likes, comments, shares, saves, thumbnail, url, media_url, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET views=excluded.views, likes=excluded.likes,
               comments=excluded.comments, shares=excluded.shares, saves=excluded.saves,
               title=excluded.title, media_url=COALESCE(excluded.media_url, videos.media_url),
               thumbnail=COALESCE(excluded.thumbnail, videos.thumbnail),
               duration_s=CASE WHEN excluded.duration_s > 0 THEN excluded.duration_s ELSE videos.duration_s END,
               fetched_at=excluded.fetched_at""",
        (p.id, p.platform, p.creator_id, p.title, p.description[:2000], p.published_at, p.duration_s,
         p.views, p.likes, p.comments, p.shares, p.saves, p.thumbnail, p.url, p.media_url, _iso(_now())),
    )
    if genre and keyword:
        conn.execute("INSERT OR IGNORE INTO hits (video_id, genre, keyword) VALUES (?, ?, ?)",
                     (p.id, genre, keyword))


def _medians(platform: str, posts: list[Post]) -> tuple[float | None, float | None, int]:
    settled = [p for p in posts if p.views is not None and p.published_at
               and scoring.age_days(p.published_at) >= MIN_AGE_FOR_MEDIAN_DAYS]
    short = [p.views for p in settled if scoring.bucket(platform, p.duration_s) == "short"]
    long_ = [p.views for p in settled if scoring.bucket(platform, p.duration_s) == "long"]
    return scoring.channel_median(short), scoring.channel_median(long_), len(settled)


def add_snapshot(conn, channel_id: str, followers: int | None, taken_at: str | None = None) -> None:
    if followers is None:
        return
    taken_at = taken_at or _iso(_now())
    last = conn.execute("SELECT MAX(taken_at) FROM follower_snapshots WHERE channel_id = ?",
                        (channel_id,)).fetchone()[0]
    if last and taken_at > last and (scoring.parse_time(taken_at) - scoring.parse_time(last)) < timedelta(
            hours=SNAPSHOT_MIN_GAP_HOURS):
        return
    conn.execute("INSERT OR REPLACE INTO follower_snapshots VALUES (?, ?, ?)",
                 (channel_id, taken_at, followers))


def save_profile(conn, profile: Profile, provider=None, tracked: bool | None = None) -> None:
    c = profile.creator
    med_s = med_l = None
    sample = 0
    if profile.recent_posts is not None:
        med_s, med_l, sample = _medians(c.platform, profile.recent_posts)
    conn.execute(
        """INSERT INTO channels (id, platform, handle, title, url, followers, median_views_short,
               median_views_long, sample_size, tracked, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET handle=COALESCE(excluded.handle, channels.handle),
               title=COALESCE(excluded.title, channels.title), url=COALESCE(excluded.url, channels.url),
               followers=excluded.followers,
               median_views_short=COALESCE(excluded.median_views_short, channels.median_views_short),
               median_views_long=COALESCE(excluded.median_views_long, channels.median_views_long),
               sample_size=MAX(excluded.sample_size, COALESCE(channels.sample_size, 0)),
               tracked=MAX(excluded.tracked, channels.tracked), updated_at=excluded.updated_at""",
        (c.id, c.platform, c.handle, c.title, c.url, c.followers, med_s, med_l, sample,
         1 if tracked else 0, _iso(_now())),
    )
    for p in profile.recent_posts or []:
        save_post(conn, p)
    _sync_demo_history(conn, provider, [c.id])
    add_snapshot(conn, c.id, c.followers)


def _sync_demo_history(conn, provider, creator_ids) -> None:
    """Demo data comes with a synthetic follower history. Real sources have none:
    they build it up one snapshot per refresh."""
    history = getattr(provider, "follower_history", None)
    if not history:
        return
    for cid in creator_ids:
        conn.execute("DELETE FROM follower_snapshots WHERE channel_id = ?", (cid,))
        conn.executemany("INSERT OR IGNORE INTO follower_snapshots VALUES (?, ?, ?)",
                         [(cid, t, f) for t, f in history(cid)])


# --- metrics ------------------------------------------------------------------

def enrich(row: dict, settings) -> dict:
    """Add computed metrics to a joined video + channel row."""
    platform = row.get("platform") or "youtube"
    b = scoring.bucket(platform, row.get("duration_s") or 0)
    med = row.get("median_views_short") if b == "short" else row.get("median_views_long")
    score = scoring.outlier_score(row.get("views"), med)
    followers = row.get("followers")
    out = dict(row)
    is_short_form = platform != "youtube" or scoring.is_short(row.get("duration_s") or 0)
    out.update(
        is_short=is_short_form,
        channel_median=med,
        outlier_score=round(score, 1) if score else None,
        tier=scoring.outlier_tier(score),
        reach_ratio=round(r, 1) if (r := scoring.reach_ratio(row.get("views"), followers)) else None,
        views_per_day=round(v) if (v := scoring.views_per_day(row.get("views"), row.get("published_at"))) else None,
        engagement_rate=scoring.engagement_rate(row.get("views"), row.get("likes"), row.get("comments"),
                                                row.get("shares"), row.get("saves")),
        qualifies=bool(
            score is not None
            and score >= settings.min_outlier_score
            and (row.get("views") or 0) >= settings.min_views
            and followers is not None
            and settings.min_followers <= followers <= settings.max_followers
            and (is_short_form or not settings.shorts_only)
        ),
    )
    return out


def update_keyword_stats(conn, cfg: Config, platform: str, genre: str, keyword: str,
                         supply: int | None) -> dict:
    rows = conn.execute(
        """SELECT v.*, c.followers, c.median_views_short, c.median_views_long
           FROM hits h JOIN videos v ON v.id = h.video_id LEFT JOIN channels c ON c.id = v.channel_id
           WHERE h.genre = ? AND h.keyword = ? AND v.platform = ?""",
        (genre, keyword, platform),
    ).fetchall()
    enriched = [e for e in (enrich(dict(r), cfg.settings) for r in rows)
                if e["is_short"] or not cfg.settings.shorts_only]
    outliers = [e["outlier_score"] for e in enriched if e["qualifies"]]
    stats = {
        "platform": platform, "genre": genre, "keyword": keyword, "scanned_at": _iso(_now()),
        "supply": supply, "candidates": len(enriched), "outliers": len(outliers),
        "median_outlier": round(sorted(outliers)[len(outliers) // 2], 1) if outliers else None,
        "opportunity": scoring.opportunity_score(outliers, supply),
    }
    conn.execute(
        """INSERT INTO keyword_stats VALUES (:platform, :genre, :keyword, :scanned_at, :supply,
               :candidates, :outliers, :median_outlier, :opportunity)
           ON CONFLICT(platform, genre, keyword) DO UPDATE SET scanned_at=excluded.scanned_at,
               supply=excluded.supply, candidates=excluded.candidates, outliers=excluded.outliers,
               median_outlier=excluded.median_outlier, opportunity=excluded.opportunity""",
        stats,
    )
    return stats


# --- operations ---------------------------------------------------------------

def _stale_creators(conn, creator_ids: list[str], cache_days: int) -> list[str]:
    if not creator_ids:
        return []
    cutoff = _iso(_now() - timedelta(days=cache_days))
    fresh = {r["id"] for r in conn.execute(
        f"SELECT id FROM channels WHERE updated_at >= ? "
        f"AND id IN ({','.join('?' * len(creator_ids))})", [cutoff, *creator_ids])}
    return [c for c in creator_ids if c not in fresh]


def scan_keyword(conn, provider, cfg: Config, genre: str, keyword: str) -> dict:
    s = cfg.settings
    posts, supply = provider.search(keyword, s.lookback_days, s.results_per_keyword)
    if provider.platform == "youtube" and s.shorts_only:
        posts = [p for p in posts if scoring.is_short(p.duration_s)]
    # Only spend quota/credits on creators whose posts could qualify.
    worth_it = [p for p in posts if (p.views or 0) >= s.min_views and (
        p.creator_followers is None or s.min_followers <= p.creator_followers <= s.max_followers)]
    creator_ids = sorted({p.creator_id for p in worth_it})
    stale = _stale_creators(conn, creator_ids, s.channel_cache_days)
    if stale:
        for prof in provider.profiles(stale, s.channel_sample_size, (s.min_followers, s.max_followers)):
            save_profile(conn, prof, provider)
    known = {r["id"] for r in conn.execute("SELECT id FROM channels")}
    for p in posts:
        if p.creator_id in known:
            save_post(conn, p, genre, keyword)
    _sync_demo_history(conn, provider, sorted({p.creator_id for p in posts if p.creator_id in known}))
    return update_keyword_stats(conn, cfg, provider.platform, genre, keyword, supply)


def start_scan(cfg: Config, target: str) -> int:
    with connect(cfg.db_path) as conn:
        return conn.execute("INSERT INTO scans (target, started_at, status) VALUES (?, ?, 'running')",
                            (target, _iso(_now()))).lastrowid


def _progress(cfg: Config, scan_id: int, usage: int, message: str) -> None:
    with connect(cfg.db_path) as conn:
        conn.execute("UPDATE scans SET quota_used=?, message=? WHERE id=?", (usage, message, scan_id))


def _finish(cfg: Config, scan_id: int, status: str, usage: int, message: str) -> None:
    with connect(cfg.db_path) as conn:
        conn.execute("UPDATE scans SET finished_at=?, status=?, quota_used=?, message=? WHERE id=?",
                     (_iso(_now()), status, usage, message, scan_id))


def run_scan(cfg: Config, scan_id: int, genres: list[str], platforms: list[str],
             providers: dict | None = None) -> None:
    """Scan every keyword of the given genres on the given platforms (background-thread safe)."""
    jobs, notes, done, failed = [], [], 0, False
    for platform in platforms:
        prov = (providers or {}).get(platform) or source_for(cfg, platform).provider
        if prov is None:
            notes.append(f"{platform}: off")
        elif not prov.can_search:
            notes.append(f"{platform}: watchlist only, skipped")
        else:
            jobs += [(prov, g, k) for g in genres for k in cfg.genres.get(g, [])]
    provs = {id(p): p for p, _, _ in jobs}
    start = {k: p.usage for k, p in provs.items()}  # providers are reused; report this scan only
    spent = lambda: sum(p.usage - start[k] for k, p in provs.items())  # noqa: E731
    skip: set[str] = set()
    for prov, genre, keyword in jobs:
        if prov.platform in skip:
            continue
        try:
            with connect(cfg.db_path) as conn:
                stats = scan_keyword(conn, prov, cfg, genre, keyword)
            done += 1
            _progress(cfg, scan_id, spent(), f"{done}/{len(jobs)} · {prov.platform}: {keyword} "
                                           f"({stats['outliers']} outliers)")
        except QuotaExceeded as e:
            skip.add(prov.platform)
            notes.append(f"{prov.platform}: {e}")
        except Exception as e:  # keep other platforms going; report the failure
            log.exception("scan failed for %s %s", prov.platform, keyword)
            skip.add(prov.platform)
            failed = True
            notes.append(f"{prov.platform} stopped: {type(e).__name__}: {e}")
    usage = spent()
    status = "failed" if failed and done == 0 else ("partial" if skip else "finished")
    _finish(cfg, scan_id, status, usage, f"Scanned {done}/{len(jobs)} keyword searches. " + " · ".join(notes))


def add_creator(cfg: Config, platform: str, handle_or_url: str, provider=None) -> str:
    prov = provider or source_for(cfg, platform).provider
    if prov is None:
        raise ProviderUnavailable(f"{platform} is turned off or has no data source.")
    profile = prov.lookup_creator(handle_or_url, cfg.settings.channel_sample_size)
    with connect(cfg.db_path) as conn:
        save_profile(conn, profile, prov, tracked=True)
    return profile.creator.id


def refresh_creators(cfg: Config, scan_id: int, creator_ids: list[str] | None = None,
                     providers: dict | None = None) -> None:
    """Re-fetch tracked creators: new posts, fresh medians, and a follower snapshot."""
    with connect(cfg.db_path) as conn:
        rows = conn.execute("SELECT id, platform FROM channels WHERE tracked = 1").fetchall() \
            if creator_ids is None else conn.execute(
                f"SELECT id, platform FROM channels WHERE id IN ({','.join('?' * len(creator_ids))})",
                creator_ids).fetchall()
    by_platform: dict[str, list[str]] = {}
    for r in rows:
        by_platform.setdefault(r["platform"], []).append(r["id"])
    done, notes, used = 0, [], []
    for platform, ids in by_platform.items():
        prov = (providers or {}).get(platform) or source_for(cfg, platform).provider
        if prov is None:
            notes.append(f"{platform}: no data source")
            continue
        used.append((prov, prov.usage))
        try:
            for i in range(0, len(ids), 20):
                profs = prov.profiles(ids[i:i + 20], cfg.settings.channel_sample_size, None)
                with connect(cfg.db_path) as conn:
                    for prof in profs:
                        save_profile(conn, prof, prov)
                done += len(profs)
                _progress(cfg, scan_id, sum(p.usage - u0 for p, u0 in used),
                          f"{done}/{len(rows)} creators refreshed")
        except Exception as e:
            log.exception("refresh failed for %s", platform)
            notes.append(f"{platform}: {type(e).__name__}: {e}")
    _finish(cfg, scan_id, "partial" if notes else "finished", sum(p.usage - u0 for p, u0 in used),
            f"Refreshed {done}/{len(rows)} creators. " + " · ".join(notes))
