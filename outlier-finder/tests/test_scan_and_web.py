"""End-to-end: run a real scan through the fake client, then hit the web pages."""
import importlib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import load_config
from app.db import connect, init_db
from app.demo import FakeYouTubeClient
from app.scanner import run_scan, start_scan
from app import queries


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.setenv("OUTLIER_DB", str(tmp_path / "t.db"))
    c = load_config()
    c.api_key = None
    c.genres = {"fitness": ["abs workout", "fat loss routine"], "ai": ["ai tools"]}
    init_db(c.db_path)
    return c


def test_scan_finds_qualifying_outliers(cfg):
    client = FakeYouTubeClient()
    scan_id = start_scan(cfg, "all")
    run_scan(cfg, scan_id, list(cfg.genres), client)

    scan = queries.recent_scans(cfg)[0]
    assert scan["status"] == "finished", scan["message"]
    assert scan["quota_used"] == client.quota_used >= 300  # 3 searches × 100

    rows = queries.list_outliers(cfg)
    assert rows, "expected some outliers in synthetic data"
    s = cfg.settings
    for r in rows:
        assert r["outlier_score"] >= s.min_outlier_score
        assert s.min_subscribers <= r["subscribers"] <= s.max_subscribers
        assert r["views"] >= s.min_views and r["is_short"]
    # sorted by outlier score, descending
    scores = [r["outlier_score"] for r in rows]
    assert scores == sorted(scores, reverse=True)

    opps = queries.keyword_opportunities(cfg)
    assert {o["keyword"] for o in opps} == {"abs workout", "fat loss routine", "ai tools"}
    assert all(o["supply"] > 0 for o in opps)


def test_big_channels_skip_median_lookup(cfg):
    """Channels above max_subscribers shouldn't cost quota for medians."""
    run_scan(cfg, start_scan(cfg, "ai"), ["ai"], FakeYouTubeClient())
    with connect(cfg.db_path) as conn:
        big = conn.execute("SELECT * FROM channels WHERE subscribers > ?",
                           (cfg.settings.max_subscribers,)).fetchall()
    assert big and all(r["median_views_short"] is None for r in big)


def test_rescan_uses_channel_cache(cfg):
    client = FakeYouTubeClient()
    run_scan(cfg, start_scan(cfg, "ai"), ["ai"], client)
    first = client.quota_used
    run_scan(cfg, start_scan(cfg, "ai"), ["ai"], client)
    # second run: search (100) + one videos call, no channel refreshes
    assert client.quota_used - first <= 102


def test_quota_exceeded_is_reported(cfg):
    from app.youtube import QuotaExceeded

    class Broke(FakeYouTubeClient):
        def search(self, *a, **k):
            raise QuotaExceeded("quota exceeded")

    run_scan(cfg, start_scan(cfg, "ai"), ["ai"], Broke())
    assert queries.recent_scans(cfg)[0]["status"] == "quota_exceeded"


def test_web_pages_render(cfg, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "cfg", cfg)
    run_scan(cfg, start_scan(cfg, "all"), list(cfg.genres), FakeYouTubeClient())
    web = TestClient(main.app)

    home = web.get("/")
    assert home.status_code == 200 and "Demo mode" in home.text and "×" in home.text
    assert web.get("/?genre=fitness&keyword=abs+workout&sort=reach").status_code == 200
    assert web.get("/opportunities").status_code == 200
    assert web.get("/scans").status_code == 200

    vid = queries.list_outliers(cfg)[0]["id"]
    detail = web.get(f"/video/{vid}")
    assert detail.status_code == 200 and "outlier score" in detail.text
    assert web.get("/video/nope").status_code == 404

    api = web.get("/api/outliers?limit=5").json()
    assert len(api) <= 5 and "outlier_score" in api[0]
    assert web.get("/api/opportunities").json()
