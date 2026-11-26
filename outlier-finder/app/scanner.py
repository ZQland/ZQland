"""Keyword scans, creator tracking and follower snapshots across all platforms."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from . import scoring
from .config import Config
from .db import connect
from .models import Post, Profile
from .providers import source_for
from .providers.base import ProviderUnavailable
from .providers.youtube import QuotaExceeded

log = logging.getLogger(__name__)

MIN_AGE_FOR_MEDIAN_DAYS = 3   # fresh uploads are still growing; leave them out of the baseline
MIN_POSTS_FOR_MEDIAN = 5      # fewer settled posts than this = baseline too thin to score against
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
    return (scoring.channel_median(short, MIN_POSTS_FOR_MEDIAN),
            scoring.channel_median(long_, MIN_POSTS_FOR_MEDIAN), len(settled))


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


MEDIAN_WINDOW = 30  # a creator's median uses up to this many of their latest stored posts


def _medians_from_db(conn, channel_id: str, platform: str) -> tuple[float | None, float | None, int]:
    """Medians over the creator's latest stored posts, so a refresh that fetches only the newest
    few posts still compares against a full baseline."""
    rows = conn.execute(
        "SELECT views, published_at, duration_s FROM videos WHERE channel_id = ? AND views IS NOT NULL "
        "AND published_at IS NOT NULL ORDER BY published_at DESC LIMIT ?", (channel_id, MEDIAN_WINDOW * 2)).fetchall()
    posts = [Post(id="", platform=platform, creator_id=channel_id, title="", description="",
                  published_at=r["published_at"], duration_s=r["duration_s"] or 0, views=r["views"])
             for r in rows]
    settled = [p for p in posts if scoring.age_days(p.published_at) >= MIN_AGE_FOR_MEDIAN_DAYS][:MEDIAN_WINDOW]
    return _medians(platform, settled)


def save_profile(conn, profile: Profile, provider=None, tracked: bool | None = None,
                 genre: str | None = None) -> None:
    c = profile.creator
    med_s = med_l = None
    sample = 0
    if profile.recent_posts is not None:
        med_s, med_l, sample = _medians(c.platform, profile.recent_posts)
    conn.execute(
        """INSERT INTO channels (id, platform, handle, title, url, followers, median_views_short,
               median_views_long, sample_size, tracked, updated_at, genre)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET handle=COALESCE(excluded.handle, channels.handle),
               genre=COALESCE(channels.genre, excluded.genre),
               title=COALESCE(excluded.title, channels.title), url=COALESCE(excluded.url, channels.url),
               followers=excluded.followers,
               median_views_short=COALESCE(excluded.median_views_short, channels.median_views_short),
               median_views_long=COALESCE(excluded.median_views_long, channels.median_views_long),
               sample_size=MAX(excluded.sample_size, COALESCE(channels.sample_size, 0)),
               tracked=MAX(excluded.tracked, channels.tracked), updated_at=excluded.updated_at""",
        (c.id, c.platform, c.handle, c.title, c.url, c.followers, med_s, med_l, sample,
         1 if tracked else 0, _iso(_now()), genre),
    )
    for p in profile.recent_posts or []:
        save_post(conn, p)
    if profile.recent_posts:
        med_s, med_l, sample = _medians_from_db(conn, c.id, c.platform)
        conn.execute("""UPDATE channels SET median_views_short = COALESCE(?, median_views_short),
                            median_views_long = COALESCE(?, median_views_long),
                            sample_size = MAX(?, COALESCE(sample_size, 0)) WHERE id = ?""",
                     (med_s, med_l, sample, c.id))
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


def _keep(cfg: Config, provider, posts: list[Post]) -> list[Post]:
    s = cfg.settings
    if provider.platform == "youtube" and s.shorts_only:
        posts = [p for p in posts if scoring.is_short(p.duration_s)]
    if s.language:
        posts = [p for p in posts if scoring.language_matches(p.language, s.language, p.title)]
    return posts


def _worth_looking_up(cfg: Config, posts: list[Post]) -> list[Post]:
    """Posts whose creator could qualify: only these creators are worth credits/quota."""
    s = cfg.settings
    return [p for p in posts if (p.views or 0) >= s.min_views and (
        p.creator_followers is None or s.min_followers <= p.creator_followers <= s.max_followers)]


def _save_hits(conn, cfg: Config, provider, genre: str, keyword: str, posts: list[Post],
               supply: int | None) -> dict:
    known = {r["id"] for r in conn.execute("SELECT id FROM channels")}
    for p in posts:
        if p.creator_id in known:
            save_post(conn, p, genre, keyword)
    _sync_demo_history(conn, provider, sorted({p.creator_id for p in posts if p.creator_id in known}))
    return update_keyword_stats(conn, cfg, provider.platform, genre, keyword, supply)


def scan_keyword(conn, provider, cfg: Config, genre: str, keyword: str) -> dict:
    """One keyword on one platform (kept for scripts; scans use the batched path below)."""
    s = cfg.settings
    posts, supply = provider.search(keyword, s.lookback_days, s.results_per_keyword)
    posts = _keep(cfg, provider, posts)
    creator_ids = sorted({p.creator_id for p in _worth_looking_up(cfg, posts)})
    stale = _stale_creators(conn, creator_ids, s.channel_cache_days)
    if stale:
        for prof in provider.profiles(stale, s.channel_sample_size, (s.min_followers, s.max_followers)):
            save_profile(conn, prof, provider, genre=genre)
    return _save_hits(conn, cfg, provider, genre, keyword, posts, supply)


def start_scan(cfg: Config, target: str) -> int:
    with connect(cfg.db_path) as conn:
        return conn.execute("INSERT INTO scans (target, started_at, status) VALUES (?, ?, 'running')",
                            (target, _iso(_now()))).lastrowid


def _progress(cfg: Config, scan_id: int, usage: int, message: str) -> None:
    with connect(cfg.db_path) as conn:
        conn.execute("UPDATE scans SET quota_used=?, message=? WHERE id=? AND status='running'",
                     (usage, message, scan_id))


def cancel_scan(cfg: Config, scan_id: int) -> bool:
    """Mark a running scan cancelled. Workers stop at the next step and abort Apify runs in progress.

    Also clears scans orphaned by a server restart, which would otherwise block new scans forever."""
    with connect(cfg.db_path) as conn:
        return conn.execute("UPDATE scans SET status='cancelled', finished_at=? WHERE id=? AND status='running'",
                            (_iso(_now()), scan_id)).rowcount > 0


def _cancelled(cfg: Config, scan_id: int) -> bool:
    with connect(cfg.db_path) as conn:
        row = conn.execute("SELECT status FROM scans WHERE id=?", (scan_id,)).fetchone()
    return row is not None and row["status"] == "cancelled"


def _finish(cfg: Config, scan_id: int, status: str, usage: int, message: str, cost_usd: float = 0.0) -> None:
    with connect(cfg.db_path) as conn:
        conn.execute("UPDATE scans SET finished_at=?, status=?, quota_used=?, message=?, cost_usd=? WHERE id=?",
                     (_iso(_now()), status, usage, message, round(cost_usd, 4), scan_id))


# --- scans ----------------------------------------------------------------------

def wide_scan_enabled(cfg: Config) -> bool:
    with connect(cfg.db_path) as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key = 'wide_scan'").fetchone()
    return bool(row and row["value"] == "1")


def set_wide_scan(cfg: Config, on: bool) -> None:
    with connect(cfg.db_path) as conn:
        conn.execute("INSERT INTO app_state (key, value) VALUES ('wide_scan', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", ("1" if on else "0",))


def _apify_client(provider):
    client = getattr(provider, "client", None)
    return client if hasattr(client, "budget") else None


class _Stop(Exception):
    pass


class _PlatformScan:
    """Scans one platform: optional keyword discovery, then the creator list. Runs in its own thread."""

    def __init__(self, cfg: Config, scan_id: int, provider, genres: list[str] | None, wide: bool,
                 creator_ids: list[str] | None, force: bool, budget, report):
        self.cfg, self.scan_id, self.prov = cfg, scan_id, provider
        self.genres, self.wide, self.creator_ids, self.force = genres, wide, creator_ids, force
        self.budget, self.report = budget, report
        self.platform = provider.platform
        self.apify = _apify_client(provider)
        self.start_usage = provider.usage
        self.notes: list[str] = []
        self.keywords_done = self.creators_done = 0
        self.failed = self.cancelled = self.stopped = False
        self.fetched: set[str] = set()

    # sample sizes: Apify bills per post, YouTube costs the same for 5 or 50
    def _baseline(self) -> int:
        return self.cfg.apify.baseline_posts if self.apify else self.cfg.settings.channel_sample_size

    def _refresh(self) -> int:
        return self.cfg.apify.refresh_posts if self.apify else self.cfg.settings.channel_sample_size

    def _check(self) -> None:
        if _cancelled(self.cfg, self.scan_id):
            self.cancelled = True
            raise _Stop

    def _status(self, text: str) -> None:
        self.report(self.platform, text, self.prov.usage - self.start_usage)

    def run(self) -> "_PlatformScan":
        old = (self.apify.budget, self.apify.should_stop) if self.apify else None
        if self.apify:
            self.apify.budget = self.budget
            self.apify.should_stop = lambda: _cancelled(self.cfg, self.scan_id)
        try:
            if self.wide and self.creator_ids is None:
                if self.prov.can_search:
                    self._keywords()
                else:
                    self.notes.append(f"{self.platform}: creator list only, no keyword search")
            self._creators()
        except _Stop:
            pass
        except Exception as e:  # noqa: BLE001 - report the reason, keep other platforms going
            from .budget import BudgetExceeded
            if isinstance(e, (BudgetExceeded, QuotaExceeded)):
                self.stopped = True
                self.notes.append(f"{self.platform}: {e}")
            else:
                log.exception("scan failed on %s", self.platform)
                self.failed = True
                self.notes.append(f"{self.platform} stopped: {type(e).__name__}: {e}")
        finally:
            if self.apify:
                self.apify.budget, self.apify.should_stop = old
            if _cancelled(self.cfg, self.scan_id):
                self.cancelled = True
        return self

    # --- keyword discovery (wide scan) ---
    def _keywords(self) -> None:
        s = self.cfg.settings
        jobs = [(g, k) for g in (self.genres or list(self.cfg.genres)) for k in self.cfg.genres.get(g, [])]
        if not jobs:
            return
        queries = list(dict.fromkeys(k for _, k in jobs))
        self._status(f"searching {len(queries)} keywords")
        if hasattr(self.prov, "search_many"):  # one run for every keyword
            results = self.prov.search_many(queries, s.lookback_days, s.results_per_keyword)
        else:
            results = {}
            for q in queries:  # official APIs: one request per keyword, so stop between them
                self._check()
                results[q] = self.prov.search(q, s.lookback_days, s.results_per_keyword)
        self._check()
        results = {q: (_keep(self.cfg, self.prov, posts), supply) for q, (posts, supply) in results.items()}

        # Every creator worth a look, across all keywords, in one lookup.
        candidates: dict[str, tuple[float, str]] = {}  # creator -> (best views/followers, genre)
        for g, k in jobs:
            for p in _worth_looking_up(self.cfg, results.get(k, ([], None))[0]):
                score = (p.views or 0) / max(p.creator_followers or 1, 1)
                if p.creator_id not in candidates or score > candidates[p.creator_id][0]:
                    candidates[p.creator_id] = (score, g)
        with connect(self.cfg.db_path) as conn:
            stale = _stale_creators(conn, sorted(candidates), s.channel_cache_days)
        if self.apify:  # credits: look up the most promising new creators only
            stale = sorted(stale, key=lambda c: -candidates[c][0])[:self.cfg.apify.new_creators_per_scan]
        if stale:
            self._check()
            self._status(f"looking up {len(stale)} new creators")
            profiles = self.prov.profiles(stale, self._baseline(), (s.min_followers, s.max_followers))
            with connect(self.cfg.db_path) as conn:
                for prof in profiles:
                    save_profile(conn, prof, self.prov, genre=candidates.get(prof.creator.id, (0, None))[1])
                    self.fetched.add(prof.creator.id)
        with connect(self.cfg.db_path) as conn:
            for g, k in jobs:
                posts, supply = results.get(k, ([], None))
                _save_hits(conn, self.cfg, self.prov, g, k, posts, supply)
                self.keywords_done += 1

    # --- the creator list ---
    def _due(self) -> list[dict]:
        where, params = ["platform = ?"], [self.platform]
        if self.creator_ids is not None:
            where.append(f"id IN ({','.join('?' * len(self.creator_ids))})")
            params += self.creator_ids
        else:
            where.append("tracked = 1")
            if self.genres is not None:
                where.append(f"genre IN ({','.join('?' * len(self.genres))})")
                params += self.genres
        if not self.force:
            where.append("(updated_at IS NULL OR updated_at < ?)")
            params.append(_iso(_now() - timedelta(hours=self.cfg.apify.refresh_hours)))
        with connect(self.cfg.db_path) as conn:
            rows = [dict(r) for r in conn.execute(
                f"SELECT id, sample_size FROM channels WHERE {' AND '.join(where)} ORDER BY updated_at", params)]
        return [r for r in rows if r["id"] not in self.fetched]

    def _creators(self) -> None:
        due = self._due()
        if not due:
            return
        new = [r["id"] for r in due if (r["sample_size"] or 0) < MIN_POSTS_FOR_MEDIAN]
        known = [r["id"] for r in due if (r["sample_size"] or 0) >= MIN_POSTS_FOR_MEDIAN]
        # One run per group: newcomers need a full baseline, known creators only their newest posts.
        for ids, n, label in ((known, self._refresh(), "checking"), (new, self._baseline(), "baselining")):
            if not ids:
                continue
            self._check()
            self._status(f"{label} {len(ids)} creators")
            profiles = self.prov.profiles(ids, n, None)
            with connect(self.cfg.db_path) as conn:
                for prof in profiles:
                    save_profile(conn, prof, self.prov)
            self.creators_done += len(profiles)
            if len(profiles) < len(ids):
                self.notes.append(f"{self.platform}: {len(ids) - len(profiles)} creators returned nothing")


def run_scan(cfg: Config, scan_id: int, genres: list[str] | None, platforms: list[str],
             providers: dict | None = None, wide: bool | None = None,
             creator_ids: list[str] | None = None, force: bool = False) -> None:
    """Scan platforms in parallel (background-thread safe).

    Default: the creator list. Tracked creators in `genres` (None = all) whose posts are older
    than apify.refresh_hours are re-fetched, one batched lookup per platform.
    Wide scan (switch on the Scans page): first search every keyword of the genres in one batched
    run per platform, look up the most promising new creators, then the creator list.
    """
    from concurrent.futures import ThreadPoolExecutor
    from threading import Lock

    from .budget import ApifyBudget

    wide = wide_scan_enabled(cfg) if wide is None else wide
    scans, notes = [], []
    for platform in platforms:
        prov = (providers or {}).get(platform) or source_for(cfg, platform).provider
        if prov is None:
            notes.append(f"{platform}: off")
        else:
            scans.append(prov)
    n_apify = sum(1 for p in scans if _apify_client(p)) or 1
    progress: dict[str, tuple[str, int]] = {}
    lock = Lock()

    def report(platform: str, text: str, usage: int) -> None:
        with lock:
            progress[platform] = (text, usage)
            msg = " · ".join(f"{p}: {t}" for p, (t, _) in progress.items())
            total = sum(u for _, u in progress.values())
        _progress(cfg, scan_id, total, msg)

    def one(prov) -> _PlatformScan:
        # The per-scan cap is split between the Apify platforms that run side by side.
        budget = ApifyBudget(cfg, cfg.apify.max_usd_per_scan / n_apify) if _apify_client(prov) else None
        return _PlatformScan(cfg, scan_id, prov, genres, wide, creator_ids, force, budget, report).run()

    with ThreadPoolExecutor(max_workers=max(len(scans), 1)) as pool:
        results = list(pool.map(one, scans))

    usage = sum(r.prov.usage - r.start_usage for r in results)
    cost = sum(r.budget.spent for r in results if r.budget)
    notes += [n for r in results for n in r.notes]
    kw = sum(r.keywords_done for r in results)
    cr = sum(r.creators_done for r in results)
    cancelled = any(r.cancelled for r in results) or _cancelled(cfg, scan_id)
    if cancelled:
        status = "cancelled"
    elif results and all(r.failed for r in results) and not (kw or cr):
        status = "failed"
    elif any(r.failed or r.stopped for r in results):
        status = "partial"
    else:
        status = "finished"
    summary = (f"{'Cancelled. ' if cancelled else ''}{'Wide scan: ' + str(kw) + ' keyword searches, ' if wide and creator_ids is None else ''}"
               f"{cr} creators checked")
    if not cr and not kw and not notes:
        summary += " (all were checked recently; see apify.refresh_hours)" if not force else ""
    _finish(cfg, scan_id, status, usage, summary + ". " + " · ".join(notes), cost)


def refresh_creators(cfg: Config, scan_id: int, creator_ids: list[str] | None = None,
                     providers: dict | None = None, force: bool | None = None) -> None:
    """Re-fetch tracked creators (or the given ones): new posts, fresh medians, a follower snapshot.
    Naming creators forces the refresh; refreshing all respects apify.refresh_hours."""
    with connect(cfg.db_path) as conn:
        rows = conn.execute("SELECT DISTINCT platform FROM channels WHERE tracked = 1").fetchall() \
            if creator_ids is None else conn.execute(
                f"SELECT DISTINCT platform FROM channels WHERE id IN ({','.join('?' * len(creator_ids))})",
                creator_ids).fetchall()
    run_scan(cfg, scan_id, None, [r["platform"] for r in rows], providers, wide=False,
             creator_ids=creator_ids, force=creator_ids is not None if force is None else force)


# --- adding creators --------------------------------------------------------------

def platform_of(text: str, default: str) -> str:
    t = text.lower()
    for host, platform in (("tiktok.com", "tiktok"), ("instagram.com", "instagram"),
                           ("youtube.com", "youtube"), ("youtu.be", "youtube")):
        if host in t:
            return platform
    return default


_SEPARATORS = re.compile(r"[\s,;|]+")


def split_handles(text: str) -> list[str]:
    """Pasted handles/URLs -> a de-duplicated list. Splits on new lines, spaces, tabs, commas,
    semicolons and pipes, and on run-together handles ("@a@b"). Order is kept."""
    out, seen = [], set()
    for chunk in _SEPARATORS.split(text or ""):
        for part in re.split(r"(?<=[\w.])(?=@)", chunk):
            part = part.strip().rstrip(".").strip("\"'()[]<>").rstrip(".")
            if not part or part == "@":
                continue
            key = part.lower().rstrip("/")
            if key not in seen:
                seen.add(key)
                out.append(part)
    return out


def _tracked_ids(cfg: Config) -> set[str]:
    with connect(cfg.db_path) as conn:
        return {r["id"] for r in conn.execute("SELECT id FROM channels WHERE tracked = 1")}


def _guess_id(platform: str, entry: str) -> str | None:
    """The channel id an entry will get, where it can be known without a lookup (TikTok, Instagram)."""
    from .providers.apify import _ig_username, _tiktok_username
    if platform == "tiktok":
        return f"tiktok:{_tiktok_username(entry)}"
    if platform == "instagram":
        return f"instagram:{_ig_username(entry)}"
    return None


def add_creator(cfg: Config, platform: str, handle_or_url: str, provider=None, genre: str | None = None) -> str:
    prov = provider or source_for(cfg, platform).provider
    if prov is None:
        raise ProviderUnavailable(f"{platform} is turned off or has no data source.")
    sample = cfg.apify.baseline_posts if _apify_client(prov) else cfg.settings.channel_sample_size
    profile = prov.lookup_creator(handle_or_url, sample)
    with connect(cfg.db_path) as conn:
        save_profile(conn, profile, prov, tracked=True, genre=genre)
        if genre:
            conn.execute("UPDATE channels SET genre = ? WHERE id = ?", (genre, profile.creator.id))
    return profile.creator.id


def add_creators(cfg: Config, scan_id: int, entries: list[str], default_platform: str,
                 genre: str | None = None, providers: dict | None = None) -> None:
    """Track many creators at once: one lookup run per platform, within the per-scan budget."""
    from .budget import ApifyBudget

    by_platform: dict[str, list[str]] = {}
    tracked, skipped = _tracked_ids(cfg), 0
    for e in entries:
        if e.strip():
            platform = platform_of(e, default_platform)
            if _guess_id(platform, e.strip()) in tracked:
                skipped += 1  # already tracked: don't pay to look it up again
                continue
            by_platform.setdefault(platform, []).append(e.strip())
    added, notes, cost, usage = [], [], 0.0, 0
    if skipped:
        notes.append(f"{skipped} already tracked, skipped")
    for platform, handles in by_platform.items():
        prov = (providers or {}).get(platform) or source_for(cfg, platform).provider
        if prov is None:
            notes.append(f"{platform}: no data source")
            continue
        client, u0 = _apify_client(prov), prov.usage
        budget = ApifyBudget(cfg, cfg.apify.max_usd_per_import) if client else None
        old = client.budget if client else None
        if client:
            client.budget = budget
        try:
            if hasattr(prov, "lookup_creators"):
                profiles = prov.lookup_creators(handles, cfg.apify.baseline_posts if client
                                                else cfg.settings.channel_sample_size)
            else:
                profiles = []
                for h in handles:
                    try:
                        profiles.append(prov.lookup_creator(h, cfg.settings.channel_sample_size))
                    except ProviderUnavailable as e:
                        notes.append(str(e))
            with connect(cfg.db_path) as conn:
                for prof in profiles:
                    save_profile(conn, prof, prov, tracked=True, genre=genre)
                    if genre:
                        conn.execute("UPDATE channels SET genre = ? WHERE id = ?", (genre, prof.creator.id))
                    added.append(prof.creator.handle or prof.creator.id)
            if len(profiles) < len(handles):
                notes.append(f"{platform}: {len(handles) - len(profiles)} of {len(handles)} not found")
        except Exception as e:  # noqa: BLE001
            from .budget import BudgetExceeded
            plain = isinstance(e, (BudgetExceeded, ProviderUnavailable, QuotaExceeded))
            notes.append(f"{platform}: {e}" if plain else f"{platform}: {type(e).__name__}: {e}")
        finally:
            if client:
                client.budget = old
            cost += budget.spent if budget else 0.0
            usage += prov.usage - u0
    real_notes = [n for n in notes if not n.endswith("already tracked, skipped")]
    status = ("finished" if added and not real_notes else "partial" if added
              else "finished" if skipped and not real_notes else "failed")
    _finish(cfg, scan_id, status, usage,
            f"Tracked {len(added)} creators{' (' + genre + ')' if genre else ''}. " + " · ".join(notes), cost)
