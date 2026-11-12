"""Pure functions for outlier and opportunity scoring (no I/O, easy to test)."""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from statistics import median

SHORTS_MAX_SECONDS = 180  # YouTube Shorts can be up to 3 minutes

_DURATION_RE = re.compile(
    r"^P(?:(?P<d>\d+)D)?(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?)?$"
)


def parse_duration(iso: str | None) -> int:
    """Convert an ISO-8601 duration like 'PT1M5S' to seconds (0 if unknown)."""
    if not iso:
        return 0
    m = _DURATION_RE.match(iso)
    if not m:
        return 0
    parts = {k: int(v) if v else 0 for k, v in m.groupdict().items()}
    return parts["d"] * 86400 + parts["h"] * 3600 + parts["m"] * 60 + parts["s"]


def is_short(duration_s: int) -> bool:
    return 0 < duration_s <= SHORTS_MAX_SECONDS


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def age_days(published_at: str, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    return max((now - parse_time(published_at)).total_seconds() / 86400, 0.0)


def channel_median(view_counts: list[int], min_posts: int = 3) -> float | None:
    """Median views of a channel's recent uploads; None if too few to trust."""
    counts = [v for v in view_counts if v is not None]
    if len(counts) < min_posts:
        return None
    return float(median(counts))


def bucket(platform: str, duration_s: int) -> str:
    """Which median to compare against. YouTube mixes Shorts and long-form;
    TikTok and Instagram Reels are all short-form, so they use one bucket."""
    if platform != "youtube":
        return "short"
    return "short" if is_short(duration_s) else "long"


def outlier_score(views: int | None, channel_median_views: float | None) -> float | None:
    """How many times better than the channel's normal video this one did."""
    if views is None or not channel_median_views or channel_median_views <= 0:
        return None
    return views / channel_median_views


def reach_ratio(views: int | None, subscribers: int | None) -> float | None:
    """Views per subscriber (e.g. 1M views on a 10k channel = 100)."""
    if not subscribers or views is None:
        return None
    return views / subscribers


def views_per_day(views: int | None, published_at: str | None, now: datetime | None = None) -> float | None:
    if views is None or not published_at:
        return None
    return views / max(age_days(published_at, now), 1.0)


def engagement_rate(views: int | None, likes: int | None = None, comments: int | None = None,
                    shares: int | None = None, saves: int | None = None) -> float | None:
    """(likes + comments + shares + saves) ÷ views, as a percentage."""
    if not views:
        return None
    interactions = sum(x for x in (likes, comments, shares, saves) if x)
    return round(interactions / views * 100, 2)


def estimate_followers_gained(snapshots: list[tuple[str, int]], published_at: str,
                              this_views: int | None,
                              window_posts: list[tuple[str, int | None]] | None = None,
                              window_days: float = 7.0) -> dict:
    """Estimate followers a post brought in, from follower snapshots over time.

    No platform exposes this for other creators' videos, so we compare the follower
    count just before posting with ~a week later, subtract the creator's normal
    growth rate, and split the excess between posts published in that window by views.
    """
    if not snapshots or not published_at:
        return {"estimate": None, "confidence": "none",
                "note": "Track this creator to collect follower history."}
    pts = sorted((parse_time(t), f) for t, f in snapshots if f is not None)
    t0 = parse_time(published_at)
    before = [p for p in pts if t0 - _days(3) <= p[0] <= t0]
    after = [p for p in pts if t0 + _days(2) <= p[0] <= t0 + _days(window_days)]
    if not before or not after:
        return {"estimate": None, "confidence": "none",
                "note": "Needs a snapshot from just before posting and one 2–7 days after."}
    (tb, fb), (ta, fa) = before[-1], after[-1]
    history = [p for p in pts if t0 - _days(30) <= p[0] < t0 - _days(1)]
    rate, has_baseline = 0.0, False
    if len(history) >= 2 and (history[-1][0] - history[0][0]).days >= 3:
        span = (history[-1][0] - history[0][0]).total_seconds() / 86400
        rate = (history[-1][1] - history[0][1]) / span
        has_baseline = True
    span_days = (ta - tb).total_seconds() / 86400
    excess = (fa - fb) - rate * span_days
    others = [(t, v) for t, v in (window_posts or []) if t and tb <= parse_time(t) <= ta]
    total_views = sum(v or 0 for _, v in others) or (this_views or 0)
    share = (this_views or 0) / total_views if total_views else 1.0
    shared = len(others) > 1
    return {
        "estimate": max(0, round(excess * share)),
        "confidence": "medium" if has_baseline and not shared else "low",
        "window_days": round(span_days, 1),
        "baseline_per_day": round(rate, 1),
        "note": (f"Follower change over {span_days:.0f} days minus normal growth"
                 + (f", split across {len(others)} posts by views" if shared else "")
                 + ("" if has_baseline else "; no history before posting, so normal growth is unknown")
                 + "."),
    }


def _days(n: float):
    from datetime import timedelta
    return timedelta(days=n)


def outlier_tier(score: float | None) -> str:
    if score is None:
        return "unknown"
    if score >= 10:
        return "10x+"
    if score >= 5:
        return "5x+"
    if score >= 3:
        return "3x+"
    return "normal"


def opportunity_score(outlier_scores: list[float], supply: int | None) -> float | None:
    """Demand ÷ supply for one keyword.

    demand = number of outliers × median outlier score (many strong outliers = demand)
    supply = recent uploads matching the keyword (YouTube's approximate count)
    Supply is log-scaled because the count spans several orders of magnitude.
    """
    if not outlier_scores:
        return 0.0
    demand = len(outlier_scores) * median(outlier_scores)
    supply_factor = math.log10((supply or 0) + 10)
    return round(demand / supply_factor, 2)
