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
