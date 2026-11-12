from datetime import datetime, timezone

import pytest

from app import scoring


@pytest.mark.parametrize("iso,seconds", [
    ("PT45S", 45), ("PT1M5S", 65), ("PT3M", 180), ("PT1H2M3S", 3723), ("P1DT1S", 86401),
    ("", 0), (None, 0), ("garbage", 0),
])
def test_parse_duration(iso, seconds):
    assert scoring.parse_duration(iso) == seconds


def test_is_short_boundary():
    assert scoring.is_short(180)
    assert not scoring.is_short(181)
    assert not scoring.is_short(0)


def test_outlier_and_reach():
    assert scoring.outlier_score(1_000_000, 50_000) == 20
    assert scoring.outlier_score(100, None) is None
    assert scoring.outlier_score(100, 0) is None
    # The user's two examples: 10k subs / 1M views and 1k subs / 100k views
    assert scoring.reach_ratio(1_000_000, 10_000) == 100
    assert scoring.reach_ratio(100_000, 1_000) == 100
    assert scoring.reach_ratio(5, None) is None


def test_channel_median_needs_three_videos():
    assert scoring.channel_median([10, 20]) is None
    assert scoring.channel_median([10, 20, 1000]) == 20


def test_views_per_day_floors_age_at_one_day():
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    assert scoring.views_per_day(1000, "2026-01-10T00:00:00Z", now) == 1000
    assert scoring.views_per_day(1000, "2026-01-05T00:00:00Z", now) == 200


def test_tiers():
    assert [scoring.outlier_tier(s) for s in (None, 2, 3, 5, 12)] == \
        ["unknown", "normal", "3x+", "5x+", "10x+"]


def test_opportunity_prefers_low_supply():
    scores = [5.0, 8.0, 12.0]
    assert scoring.opportunity_score(scores, 1_000) > scoring.opportunity_score(scores, 1_000_000)
    assert scoring.opportunity_score([], 10) == 0.0


def test_engagement_rate_and_bucket():
    assert scoring.engagement_rate(1000, 50, 10, 5, 35) == 10.0
    assert scoring.engagement_rate(1000, 50, None, None, None) == 5.0
    assert scoring.engagement_rate(0, 5) is None
    assert scoring.bucket("youtube", 600) == "long"
    assert scoring.bucket("tiktok", 600) == "short"  # TikTok/Reels: one bucket


def _snaps(start_followers, daily, jump_day=None, jump=0, days=40):
    out, f = [], start_followers
    for d in range(days):
        if d == jump_day:
            f += jump
        out.append((f"2026-01-{1 + d:02d}T00:00:00Z" if d < 31 else f"2026-02-{d - 30:02d}T00:00:00Z", f))
        f += daily
    return out


def test_followers_gained_subtracts_normal_growth():
    snaps = _snaps(10_000, 10, jump_day=22, jump=5_000)  # +10/day, +5,000 after a post on Jan 21
    est = scoring.estimate_followers_gained(snaps, "2026-01-21T12:00:00Z", 900_000)
    assert est["confidence"] == "medium"
    assert 4_900 <= est["estimate"] <= 5_100


def test_followers_gained_splits_between_posts_and_needs_snapshots():
    snaps = _snaps(10_000, 0, jump_day=22, jump=4_000)
    posts = [("2026-01-21T12:00:00Z", 300_000), ("2026-01-23T12:00:00Z", 100_000)]
    est = scoring.estimate_followers_gained(snaps, "2026-01-21T12:00:00Z", 300_000, posts)
    assert est["estimate"] == 3_000 and est["confidence"] == "low"
    assert scoring.estimate_followers_gained([], "2026-01-21T00:00:00Z", 5)["estimate"] is None
    assert scoring.estimate_followers_gained(snaps[:10], "2026-01-21T00:00:00Z", 5)["estimate"] is None


def test_language_matches_and_hidden_engagement():
    assert scoring.language_matches("en-US", "en") and scoring.language_matches(None, "en")
    assert scoring.language_matches("und", "en") and not scoring.language_matches("hi", "en")
    assert not scoring.language_matches(None, "en", "15 दिनों में तेजी से वजन घटाएं! 🔥 9 Easy Tips")
    assert scoring.language_matches(None, "en", "4 Belly Fat Exercises 🔥 #shorts")
    assert scoring.engagement_rate(1000, None, 0, 5, 5) is None
