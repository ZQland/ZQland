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


def channel_median(view_counts: list[int]) -> float | None:
    """Median views of a channel's recent uploads; None if too few to trust."""
    counts = [v for v in view_counts if v is not None]
    if len(counts) < 3:
        return None
    return float(median(counts))


def outlier_score(views: int, channel_median_views: float | None) -> float | None:
    """How many times better than the channel's normal video this one did."""
    if not channel_median_views or channel_median_views <= 0:
        return None
    return views / channel_median_views


def reach_ratio(views: int, subscribers: int | None) -> float | None:
    """Views per subscriber (e.g. 1M views on a 10k channel = 100)."""
    if not subscribers:
        return None
    return views / subscribers


def views_per_day(views: int, published_at: str, now: datetime | None = None) -> float:
    return views / max(age_days(published_at, now), 1.0)


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
