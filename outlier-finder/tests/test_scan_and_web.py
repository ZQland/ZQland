"""End-to-end: scans through fake providers on all platforms, then the web pages."""
from fastapi.testclient import TestClient

from app import queries
from app.db import connect
from app.providers import source_for
from app.providers.fake import FakeProvider
from app.providers.youtube import QuotaExceeded
from app.scanner import add_creator, refresh_creators, run_scan, start_scan

ALL = ["youtube", "tiktok", "instagram"]


def fakes():
    return {p: FakeProvider(p) for p in ALL}


def test_demo_mode_without_credentials(cfg):
    for p in ALL:
        s = source_for(cfg, p)
        assert s.demo and s.provider is not None
    cfg.platforms["tiktok"] = "official"
    s = source_for(cfg, "tiktok")
    assert s.provider is None and "no official API" in s.note


def test_scan_all_platforms_finds_outliers(cfg):
    run_scan(cfg, start_scan(cfg, "all"), list(cfg.genres), ALL, fakes())
    scan = queries.recent_scans(cfg)[0]
    assert scan["status"] == "finished", scan["message"]

    rows = queries.list_outliers(cfg)
    assert {r["platform"] for r in rows} == set(ALL)
    s = cfg.settings
    for r in rows:
        assert r["outlier_score"] >= s.min_outlier_score
        assert s.min_followers <= r["followers"] <= s.max_followers
        assert r["views"] >= s.min_views and r["is_short"]
        assert r["engagement_rate"] is not None
    scores = [r["outlier_score"] for r in rows]
    assert scores == sorted(scores, reverse=True)
    assert all(r["platform"] == "tiktok" for r in queries.list_outliers(cfg, platform="tiktok"))

    opps = queries.keyword_opportunities(cfg)
    assert len(opps) == 3 * 3  # 3 keywords × 3 platforms
    assert all(o["supply"] is None for o in opps if o["platform"] != "youtube")


def test_rescan_uses_creator_cache(cfg):
    prov = FakeProvider("youtube")
    run_scan(cfg, start_scan(cfg, "ai"), ["ai"], ["youtube"], {"youtube": prov})
    first = prov.usage
    run_scan(cfg, start_scan(cfg, "ai"), ["ai"], ["youtube"], {"youtube": prov})
    assert prov.usage - first == 100  # just the search; no creator lookups
    assert queries.recent_scans(cfg)[0]["quota_used"] == 100


def test_quota_exceeded_skips_only_that_platform(cfg):
    class Broke(FakeProvider):
        def search(self, *a, **k):
            raise QuotaExceeded("quota exceeded")

    run_scan(cfg, start_scan(cfg, "ai"), ["ai"], ["youtube", "tiktok"],
             {"youtube": Broke("youtube"), "tiktok": FakeProvider("tiktok")})
    scan = queries.recent_scans(cfg)[0]
    assert scan["status"] == "partial" and "youtube" in scan["message"]
    assert queries.list_outliers(cfg, platform="tiktok")


def test_watchlist_only_platform_is_skipped_in_scans(cfg):
    ig = FakeProvider("instagram")
    ig.can_search = False
    run_scan(cfg, start_scan(cfg, "ai"), ["ai"], ["instagram"], {"instagram": ig})
    assert "watchlist only" in queries.recent_scans(cfg)[0]["message"]


def test_track_creator_and_estimate_followers_gained(cfg):
    prov = FakeProvider("tiktok")
    cid = add_creator(cfg, "tiktok", "https://www.tiktok.com/@demo_creator_042", provider=prov)
    assert cid == "tiktok:demo_creator_042"
    creators = queries.list_creators(cfg)
    assert [c["id"] for c in creators] == [cid] and creators[0]["snapshots"] > 3

    posts = queries.creator_posts(cfg, cid)
    assert posts and posts[0]["outlier_score"] >= posts[-1]["outlier_score"]
    gained = [queries.followers_gained(cfg, p) for p in posts[:5]]
    assert any(g["estimate"] is not None for g in gained)

    refresh_creators(cfg, start_scan(cfg, "refresh"), None, {"tiktok": prov})
    assert queries.recent_scans(cfg)[0]["status"] == "finished"


def test_web_pages_render(cfg, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "cfg", cfg)
    run_scan(cfg, start_scan(cfg, "all"), list(cfg.genres), ALL, fakes())
    add_creator(cfg, "instagram", "@demo_creator_007", provider=FakeProvider("instagram"))
    web = TestClient(main.app)

    home = web.get("/")
    assert home.status_code == 200 and "demo data" in home.text and "TikTok" in home.text
    assert web.get("/?platform=instagram&genre=fitness&sort=engagement").status_code == 200
    assert web.get("/opportunities?platform=youtube").status_code == 200
    assert web.get("/scans").status_code == 200
    page = web.get("/creators")
    assert page.status_code == 200 and "demo_creator_007" in page.text
    assert web.get("/creator/instagram:demo_creator_007").status_code == 200

    vid = queries.list_outliers(cfg, platform="tiktok")[0]["id"]
    detail = web.get(f"/video/{vid}")
    assert detail.status_code == 200 and "engagement rate" in detail.text and "Analyze this video" in detail.text
    assert web.get("/video/tiktok:nope").status_code == 404

    api = web.get("/api/outliers?limit=5&platform=youtube").json()
    assert len(api) <= 5 and api[0]["platform"] == "youtube"

    r = web.post("/track/instagram:demo_creator_007", data={"tracked": 0}, follow_redirects=False)
    assert r.status_code == 303
    with connect(cfg.db_path) as conn:
        assert conn.execute("SELECT tracked FROM channels WHERE id='instagram:demo_creator_007'").fetchone()[0] == 0
