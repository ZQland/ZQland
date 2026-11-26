"""End-to-end: scans through fake providers on all platforms, then the web pages."""
import pytest
from fastapi.testclient import TestClient

from app import queries
from app.db import connect
from app.providers import source_for
from app.providers.fake import FakeProvider
from app.providers.youtube import QuotaExceeded
from app.scanner import add_creator, cancel_scan, refresh_creators, run_scan, start_scan

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
    run_scan(cfg, start_scan(cfg, "all"), list(cfg.genres), ALL, fakes(), wide=True)
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
    run_scan(cfg, start_scan(cfg, "ai"), ["ai"], ["youtube"], {"youtube": prov}, wide=True)
    first = prov.usage
    run_scan(cfg, start_scan(cfg, "ai"), ["ai"], ["youtube"], {"youtube": prov}, wide=True)
    assert prov.usage - first == 100  # just the search; no creator lookups
    assert queries.recent_scans(cfg)[0]["quota_used"] == 100


def test_quota_exceeded_skips_only_that_platform(cfg):
    class Broke(FakeProvider):
        def search(self, *a, **k):
            raise QuotaExceeded("quota exceeded")

    run_scan(cfg, start_scan(cfg, "ai"), ["ai"], ["youtube", "tiktok"],
             {"youtube": Broke("youtube"), "tiktok": FakeProvider("tiktok")}, wide=True)
    scan = queries.recent_scans(cfg)[0]
    assert scan["status"] == "partial" and "youtube" in scan["message"]
    assert queries.list_outliers(cfg, platform="tiktok")


def test_watchlist_only_platform_is_skipped_in_scans(cfg):
    ig = FakeProvider("instagram")
    ig.can_search = False
    run_scan(cfg, start_scan(cfg, "ai"), ["ai"], ["instagram"], {"instagram": ig}, wide=True)
    assert "creator list only" in queries.recent_scans(cfg)[0]["message"]


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
    run_scan(cfg, start_scan(cfg, "all"), list(cfg.genres), ALL, fakes(), wide=True)
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
    assert detail.status_code == 200 and "Engagement rate" in detail.text and "Analyze this video" in detail.text
    assert web.get("/video/tiktok:nope").status_code == 404

    api = web.get("/api/outliers?limit=5&platform=youtube").json()
    assert len(api) <= 5 and api[0]["platform"] == "youtube"

    r = web.post("/track/instagram:demo_creator_007", data={"tracked": 0}, follow_redirects=False)
    assert r.status_code == 303
    with connect(cfg.db_path) as conn:
        assert conn.execute("SELECT tracked FROM channels WHERE id='instagram:demo_creator_007'").fetchone()[0] == 0


def test_cancel_stops_scan_before_next_keyword(cfg):
    scan_id = start_scan(cfg, "all")
    prov = FakeProvider(platform="youtube")
    real_search = prov.search
    calls = []

    def search(*a, **kw):
        calls.append(a)
        cancel_scan(cfg, scan_id)  # user hits Cancel while the first keyword is in flight
        return real_search(*a, **kw)

    prov.search = search
    run_scan(cfg, scan_id, list(cfg.genres), ["youtube"], {"youtube": prov}, wide=True)
    scan = queries.recent_scans(cfg, 1)[0]
    assert len(calls) == 1
    assert scan["status"] == "cancelled"
    assert scan["message"].startswith("Cancelled.")
    assert not cancel_scan(cfg, scan_id)  # only running scans can be cancelled


def test_cancel_clears_orphaned_scan_via_web(cfg, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "cfg", cfg)
    scan_id = start_scan(cfg, "left running by a server restart")
    assert main._running()
    r = TestClient(main.app).post(f"/scans/{scan_id}/cancel", follow_redirects=False)
    assert r.status_code == 303
    assert not main._running()


def test_filters_accept_multiple_values(cfg, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "cfg", cfg)
    run_scan(cfg, start_scan(cfg, "all"), list(cfg.genres), ALL, fakes(), wide=True)

    both = queries.list_outliers(cfg, platform=["tiktok", "instagram"])
    assert {r["platform"] for r in both} == {"tiktok", "instagram"}
    assert len(both) == len(queries.list_outliers(cfg, platform="tiktok")) + \
        len(queries.list_outliers(cfg, platform="instagram"))
    kws = queries.list_outliers(cfg, keyword=["abs workout", "ai tools"])
    assert kws and all({"abs workout", "ai tools"} & set(r["keywords"].split(",")) for r in kws)
    assert {o["platform"] for o in queries.keyword_opportunities(cfg, platform=["youtube", "tiktok"])} == \
        {"youtube", "tiktok"}

    web = TestClient(main.app)
    api = web.get("/api/outliers?platform=tiktok&platform=instagram&limit=500").json()
    assert {r["platform"] for r in api} == {"tiktok", "instagram"}
    page = web.get("/?platform=tiktok&platform=instagram&genre=fitness&genre=ai")
    assert page.status_code == 200
    assert 'value="tiktok" checked' in page.text and 'value="instagram" checked' in page.text
    # A keyword from a genre that is no longer selected is ignored instead of emptying the feed.
    assert web.get("/api/outliers?genre=ai&keyword=abs+workout").status_code == 200
    stale = web.get("/?genre=ai&keyword=abs+workout")
    assert 'value="abs workout" checked' not in stale.text
    # Old single-value / blank links keep working.
    assert web.get("/?platform=&genre=&keyword=").status_code == 200
    assert web.get("/opportunities?platform=youtube&platform=tiktok&genre=ai").status_code == 200
    assert web.get("/creators?platform=tiktok&platform=instagram").status_code == 200


# --- creator-list scans, batching, budget -------------------------------------------------

class Counting(FakeProvider):
    """Fake with the batched API, counting calls the way Apify would bill runs."""

    def __init__(self, platform):
        super().__init__(platform)
        self.searches, self.batches, self.lookups = 0, [], []

    def search(self, *a, **k):
        self.searches += 1
        return super().search(*a, **k)

    def search_many(self, queries, lookback_days, limit):
        self.batches.append(list(queries))
        return {q: FakeProvider.search(self, q, lookback_days, limit) for q in queries}

    def profiles(self, creator_ids, sample_size, follower_range=None):
        self.lookups.append((list(creator_ids), sample_size))
        return super().profiles(creator_ids, sample_size, follower_range)


def _track(cfg, prov, n, genre="fitness"):
    ids = [f"{prov.platform}:demo_creator_{i:03d}" for i in range(n)]
    with connect(cfg.db_path) as conn:
        from app.scanner import save_profile
        for prof in prov.profiles(ids, 30):
            save_profile(conn, prof, prov, tracked=True, genre=genre)
        conn.execute("UPDATE channels SET updated_at = '2020-01-01T00:00:00Z'")  # due for a check
    prov.lookups.clear()
    return ids


def test_default_scan_checks_creator_list_only_in_one_lookup_per_platform(cfg):
    tt, ig = Counting("tiktok"), Counting("instagram")
    _track(cfg, tt, 12)
    _track(cfg, ig, 5, genre="ai")
    run_scan(cfg, start_scan(cfg, "all"), None, ["tiktok", "instagram"], {"tiktok": tt, "instagram": ig})
    scan = queries.recent_scans(cfg)[0]
    assert scan["status"] == "finished", scan["message"]
    assert tt.searches == 0 and not tt.batches  # no keyword or hashtag search
    assert len(tt.lookups) == 1 and len(tt.lookups[0][0]) == 12  # all creators, one run
    assert tt.lookups[0][1] == cfg.apify.refresh_posts or tt.lookups[0][1] == cfg.settings.channel_sample_size
    assert len(ig.lookups) == 1 and "17 creators checked" in scan["message"]

    # Checked just now: a second scan skips them (apify.refresh_hours) and spends nothing.
    run_scan(cfg, start_scan(cfg, "again"), None, ["tiktok"], {"tiktok": tt})
    assert len(tt.lookups) == 1 and "checked recently" in queries.recent_scans(cfg)[0]["message"]


def test_genre_scan_only_checks_that_genres_creators(cfg):
    tt = Counting("tiktok")
    _track(cfg, tt, 4, genre="fitness")
    with connect(cfg.db_path) as conn:
        conn.execute("UPDATE channels SET genre = 'ai' WHERE id = 'tiktok:demo_creator_000'")
    run_scan(cfg, start_scan(cfg, "ai"), ["ai"], ["tiktok"], {"tiktok": tt})
    assert tt.lookups[0][0] == ["tiktok:demo_creator_000"]


def test_wide_scan_searches_all_keywords_in_one_batch_per_platform(cfg):
    tt, yt = Counting("tiktok"), Counting("youtube")
    run_scan(cfg, start_scan(cfg, "all"), list(cfg.genres), ["tiktok", "youtube"], {"tiktok": tt, "youtube": yt},
             wide=True)
    assert tt.batches == [["abs workout", "fat loss routine", "ai tools"]] and tt.searches == 0
    assert len(tt.lookups) == 1  # every new creator from every keyword: one lookup
    assert len(queries.keyword_opportunities(cfg, platform="tiktok")) == 3


def test_wide_scan_caps_new_creators_on_apify(cfg):
    from app.providers.apify import ApifyClient
    tt = Counting("tiktok")
    tt.client = ApifyClient("tok")  # marks it as an Apify (billed) provider; no request is made
    cfg.apify.new_creators_per_scan = 4
    run_scan(cfg, start_scan(cfg, "all"), list(cfg.genres), ["tiktok"], {"tiktok": tt}, wide=True)
    assert len(tt.lookups[0][0]) == 4 and tt.lookups[0][1] == cfg.apify.baseline_posts


def test_budget_exceeded_pauses_platform_with_a_reason(cfg):
    from app.budget import BudgetExceeded

    class Broke(Counting):
        def profiles(self, *a, **k):
            raise BudgetExceeded("Apify paused: this month's Apify usage ($10.20) has reached the account's $10.00 limit")

    tt, yt = Broke("tiktok"), Counting("youtube")
    _track(cfg, Counting("tiktok"), 3)
    _track(cfg, yt, 3)
    run_scan(cfg, start_scan(cfg, "all"), None, ["tiktok", "youtube"], {"tiktok": tt, "youtube": yt})
    scan = queries.recent_scans(cfg)[0]
    assert scan["status"] == "partial" and "$10.00 limit" in scan["message"]
    assert len(yt.lookups) == 1  # YouTube still ran


def test_apify_budget_allowance(cfg):
    from app.budget import ApifyBudget, BudgetExceeded
    month = {"used": 4.0, "account_limit": 10.0, "resets": "2026-12-11"}
    cfg.apify.monthly_budget_usd = 5.0
    b = ApifyBudget(cfg, per_scan_usd=0.6, month=lambda *a, **k: month)
    assert b.allowance() == 0.6  # scan cap is the tightest
    b.record(0.5)
    assert b.allowance() == pytest.approx(0.1)
    month["used"] = 5.0
    with pytest.raises(BudgetExceeded, match=r"\$5.00 budget.*Resets 2026-12-11"):
        b.allowance()
    month.update(used=9.99, account_limit=10.0)
    cfg.apify.monthly_budget_usd = 50
    with pytest.raises(BudgetExceeded, match="account"):
        ApifyBudget(cfg, month=lambda *a, **k: month).allowance()


def test_refresh_fetches_few_posts_but_median_uses_stored_history(cfg):
    from app.scanner import save_profile
    from app.models import Profile
    prov = FakeProvider("tiktok")
    cid = add_creator(cfg, "tiktok", "demo_creator_010", provider=prov)
    before = queries.get_creator(cfg, cid)
    with connect(cfg.db_path) as conn:  # a refresh that only brings the 2 newest posts
        full = prov.profiles([cid], 30)[0]
        save_profile(conn, Profile(full.creator, full.recent_posts[:2]), prov)
    after = queries.get_creator(cfg, cid)
    assert after["sample_size"] >= before["sample_size"] >= 5
    assert after["median_views_short"] == pytest.approx(before["median_views_short"], rel=0.2)


def test_feed_shows_outliers_from_creator_posts(cfg):
    prov = FakeProvider("tiktok")
    _track(cfg, Counting("tiktok"), 30, genre="fitness")
    rows = queries.list_outliers(cfg)
    assert rows and all(r["source"] == "creator" for r in rows)
    assert queries.list_outliers(cfg, genre="fitness") and not queries.list_outliers(cfg, genre="ai")
    assert not queries.list_outliers(cfg, keyword="abs workout")  # keywords only match search hits
    run_scan(cfg, start_scan(cfg, "ai"), ["ai"], ["tiktok"], {"tiktok": prov}, wide=True)
    assert any(r["source"] == "keyword" for r in queries.list_outliers(cfg))


def test_suggestions_bulk_add_genre_and_toggle_via_web(cfg, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "cfg", cfg)
    monkeypatch.setattr(main, "_bg", lambda fn, *a, **k: fn(*a, **k))  # run jobs inline
    monkeypatch.setattr(main, "source_for", lambda c, p: source_for(cfg, p))
    run_scan(cfg, start_scan(cfg, "all"), list(cfg.genres), ALL, fakes(), wide=True)
    web = TestClient(main.app)

    sugg = queries.suggested_creators(cfg)
    assert sugg and not any(c["id"] in {x["id"] for x in queries.list_creators(cfg)} for c in sugg)
    page = web.get("/creators")
    assert page.status_code == 200 and "Suggested from earlier scans" in page.text
    pick = [c["id"] for c in sugg[:3]]
    web.post("/creators/track", data={"channel_id": pick, "genre": "ai"})
    assert {c["id"] for c in queries.list_creators(cfg)} == set(pick)

    web.post(f"/creator/{pick[0]}/genre", data={"genre": "fitness"})
    assert queries.get_creator(cfg, pick[0])["genre"] == "fitness"
    assert [c["id"] for c in queries.list_creators(cfg, genre="fitness")] == [pick[0]]

    r = web.post("/creators", data={"platform": "tiktok", "genre": "fitness",
                                    "handles": "demo_creator_120\nhttps://www.instagram.com/demo_creator_121/"},
                 follow_redirects=False)
    assert r.status_code == 303
    last = queries.recent_scans(cfg, 1)[0]
    assert last["status"] == "finished" and "Tracked 2 creators" in last["message"], last["message"]
    assert queries.get_creator(cfg, "instagram:demo_creator_121")["genre"] == "fitness"

    assert "Wide scan off" in web.get("/scans").text
    web.post("/settings/wide-scan", data={"on": "1"})
    assert "Wide scan on" in web.get("/scans").text


def test_split_handles_accepts_any_separator_and_dedupes():
    from app.scanner import split_handles
    pasted = "@a, @b; c | d\n@e@f\thttps://www.tiktok.com/@g/  @A \"h\" (i). x.y"
    assert split_handles(pasted) == ["@a", "@b", "c", "d", "@e", "@f", "https://www.tiktok.com/@g/", "h", "i", "x.y"]
    assert split_handles("  \n ,, ") == []


def test_bulk_add_skips_creators_already_tracked(cfg):
    from app.scanner import add_creators
    tt = Counting("tiktok")
    add_creators(cfg, start_scan(cfg, "add"), ["@demo_creator_001", "demo_creator_002"], "tiktok", None, {"tiktok": tt})
    assert len(tt.lookups) == 2
    tt.lookups.clear()
    add_creators(cfg, start_scan(cfg, "add"), ["@Demo_Creator_001", "demo_creator_002", "demo_creator_003"],
                 "tiktok", None, {"tiktok": tt})
    assert [ids for ids, _ in tt.lookups] == [["tiktok:demo_creator_003"]]  # only the new one is paid for
    last = queries.recent_scans(cfg, 1)[0]
    assert last["status"] == "finished", last["message"]
    assert "2 already tracked, skipped" in last["message"] and "Tracked 1 creators" in last["message"]

    add_creators(cfg, start_scan(cfg, "add"), ["demo_creator_003"], "tiktok", None, {"tiktok": tt})
    last = queries.recent_scans(cfg, 1)[0]
    assert last["status"] == "finished" and "1 already tracked" in last["message"]


def test_v2_database_is_migrated_without_losing_data(tmp_path):
    import sqlite3
    from app.db import init_db
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE channels (id TEXT PRIMARY KEY, platform TEXT NOT NULL, handle TEXT, title TEXT, url TEXT,
            followers INTEGER, median_views_short REAL, median_views_long REAL, sample_size INTEGER,
            tracked INTEGER NOT NULL DEFAULT 0, updated_at TEXT);
        CREATE TABLE videos (id TEXT PRIMARY KEY, platform TEXT NOT NULL, channel_id TEXT, title TEXT,
            description TEXT, published_at TEXT, duration_s INTEGER, views INTEGER, likes INTEGER, comments INTEGER,
            shares INTEGER, saves INTEGER, thumbnail TEXT, url TEXT, media_url TEXT, fetched_at TEXT);
        CREATE TABLE hits (video_id TEXT, genre TEXT, keyword TEXT, PRIMARY KEY (video_id, genre, keyword));
        CREATE TABLE scans (id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT, started_at TEXT, finished_at TEXT,
            status TEXT, quota_used INTEGER DEFAULT 0, message TEXT);
        INSERT INTO channels (id, platform, tracked) VALUES ('tiktok:a', 'tiktok', 1);
        INSERT INTO videos (id, platform, channel_id) VALUES ('tiktok:v1', 'tiktok', 'tiktok:a');
        INSERT INTO hits VALUES ('tiktok:v1', 'fitness', 'abs workout');
        INSERT INTO scans (target, status) VALUES ('old', 'finished');
        PRAGMA user_version = 2;""")
    conn.close()
    init_db(path)
    with connect(path) as conn:
        assert conn.execute("SELECT genre FROM channels WHERE id='tiktok:a'").fetchone()[0] == "fitness"
        assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 1
        assert conn.execute("SELECT cost_usd FROM scans").fetchone()[0] == 0
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3


# --- errors reach the page ---------------------------------------------------------------------

def test_errors_render_as_pages_and_json(cfg, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "cfg", cfg)
    web = TestClient(main.app, raise_server_exceptions=False)

    bad = web.get("/?min_outlier=abc")
    assert bad.status_code == 422 and "text/html" in bad.headers["content-type"]
    assert "Min outlier" in bad.text and "abc" in bad.text and "Outlier Finder" in bad.text  # full page, not raw JSON
    missing = web.get("/video/tiktok:nope")
    assert missing.status_code == 404 and "Video not found" in missing.text
    api = web.get("/api/outliers?min_outlier=abc")
    assert api.status_code == 422 and api.json()["error"]

    def boom(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(main.queries, "list_outliers", boom)
    crash = web.get("/")
    assert crash.status_code == 500 and "RuntimeError: database is locked" in crash.text


def test_failed_jobs_show_in_a_banner_until_dismissed(cfg, monkeypatch):
    import app.main as main
    from app.scanner import _finish
    monkeypatch.setattr(main, "cfg", cfg)
    web = TestClient(main.app)
    assert "recent problem" not in web.get("/").text
    _finish(cfg, start_scan(cfg, "add 1 creator"), "failed", 0, "instagram: Apify paused: budget used up")
    with connect(cfg.db_path) as conn:
        conn.execute("INSERT INTO videos (id, platform, title) VALUES ('tiktok:1', 'tiktok', 'A video')")
        conn.execute("INSERT INTO analyses (video_id, status, error, updated_at) VALUES "
                     "('tiktok:1', 'failed', 'Couldn''t download this video', strftime('%Y-%m-%dT%H:%M:%SZ','now'))")
    page = web.get("/creators").text
    assert "2 recent problems" in page and "Apify paused: budget used up" in page
    assert 'href="/video/tiktok:1"' in page and "download this video" in page
    web.post("/alerts/dismiss", data={"back": "/creators"})
    assert "recent problem" not in web.get("/").text


def test_paused_badge_when_apify_budget_is_used_up(cfg, monkeypatch):
    import app.main as main
    from app import budget
    monkeypatch.setattr(main, "cfg", cfg)
    cfg.apify_token = "tok"
    monkeypatch.setattr(budget, "apify_month", lambda *a, **k: {"used": 12.57, "account_limit": 10.0,
                                                                 "resets": "2026-12-11"})
    page = TestClient(main.app).get("/").text
    # demo providers (no real token in tests) are never shown as paused; the reason is computed
    assert budget.apify_paused(cfg).startswith("Apify budget used up: $12.57")
    assert page  # renders


def test_video_download_prefers_free_sources_and_explains_failure(cfg, tmp_path, monkeypatch):
    from app import media, pipeline
    from app.budget import BudgetExceeded
    tried = []
    monkeypatch.setattr(media, "download", lambda url, dest, token=None: tried.append(("link", url)) or (_ for _ in ()).throw(
        media.MediaError("Download failed with HTTP 403 (the link may have expired).")))
    monkeypatch.setattr(media, "ytdlp_available", lambda: True)
    monkeypatch.setattr(media, "download_youtube", lambda url, dest: tried.append(("yt-dlp", url)) or dest)

    class Prov:
        def refresh_media_url(self, post):
            raise AssertionError("Apify must not be used when yt-dlp works")

    v = {"id": "tiktok:9", "platform": "tiktok", "channel_id": "tiktok:x", "title": "", "published_at": None,
         "duration_s": 10, "views": 1, "url": "https://www.tiktok.com/@x/video/9", "media_url": "https://cdn/old.mp4"}
    assert pipeline.get_video_file(cfg, v, tmp_path, Prov()) == tmp_path / "video.mp4"
    assert [t[0] for t in tried] == ["link", "yt-dlp"]

    class Paused:
        def refresh_media_url(self, post):
            raise BudgetExceeded("Apify paused: budget used up.")
    monkeypatch.setattr(media, "ytdlp_available", lambda: False)
    with pytest.raises(media.MediaError) as e:
        pipeline.get_video_file(cfg, v, tmp_path, Paused())
    msg = str(e.value)
    assert "link may have expired" in msg and "Apify paused" in msg and "Upload the video file" in msg
    assert "pip install yt-dlp" in msg


def test_apify_storage_links_get_the_token_and_nothing_else_does(tmp_path, monkeypatch):
    import httpx
    from app import media
    seen = []

    def handler(req):
        seen.append((req.url.host, req.headers.get("authorization")))
        if req.url.host == "api.apify.com" and not req.headers.get("authorization"):
            return httpx.Response(403, json={"error": {"type": "insufficient-permissions"}})
        return httpx.Response(200, content=b"mp4")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(media.httpx, "stream", lambda method, url, headers=None, **k: client.stream(
        method, url, headers=headers))
    kv = "https://api.apify.com/v2/key-value-stores/abc/records/video.mp4"
    assert media.download(kv, tmp_path / "a.mp4", "tok").read_bytes() == b"mp4"
    media.download("https://v16.tiktokcdn.com/x.mp4", tmp_path / "b.mp4", "tok")
    assert seen == [("api.apify.com", "Bearer tok"), ("v16.tiktokcdn.com", None)]  # token never leaves Apify
    with pytest.raises(media.MediaError, match="private and needs APIFY_TOKEN"):
        media.download(kv, tmp_path / "c.mp4", None)


def test_unreachable_instagram_edge_host_falls_back_to_global_host(tmp_path, monkeypatch):
    import httpx
    from app import media
    seen = []

    def fake_stream(url, dest, headers, private):
        seen.append(httpx.URL(url).host)
        if httpx.URL(url).host != "scontent.cdninstagram.com":
            raise httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known")
        dest.write_bytes(b"mp4")
        return dest

    monkeypatch.setattr(media, "_stream_to", fake_stream)
    out = media.download("https://instagram.ftpa1-2.fna.fbcdn.net/o1/v/x.mp4?oh=sig", tmp_path / "v.mp4")
    assert out.read_bytes() == b"mp4" and seen == ["instagram.ftpa1-2.fna.fbcdn.net", "scontent.cdninstagram.com"]

    with pytest.raises(media.MediaError, match="can't look up v16.tiktokcdn.com"):  # no fallback: a plain message
        media.download("https://v16.tiktokcdn.com/x.mp4", tmp_path / "t.mp4")


def test_thumbnails_are_saved_once_and_expired_links_are_not_retried(tmp_path, monkeypatch):
    import httpx
    from app import media
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        if "expired" in url:
            return httpx.Response(403, text="URL signature expired")
        return httpx.Response(200, content=b"\xff\xd8jpg", headers={"content-type": "image/jpeg"})

    monkeypatch.setattr(media.httpx, "get", fake_get)
    p = media.cache_thumbnail(tmp_path, "instagram:abc", "https://scontent-icn2-1.cdninstagram.com/t.jpg")
    assert p and p.read_bytes() == b"\xff\xd8jpg"
    assert media.cache_thumbnail(tmp_path, "instagram:abc", "https://x/t.jpg") == p and len(calls) == 1  # from disk

    assert media.cache_thumbnail(tmp_path, "instagram:old", "https://scontent-icn2-1.cdninstagram.com/expired.jpg") is None
    n = len(calls)
    assert media.cache_thumbnail(tmp_path, "instagram:old", "https://scontent-icn2-1.cdninstagram.com/expired.jpg") is None
    assert len(calls) == n  # marked as missing: no request on every page view
    p2 = media.cache_thumbnail(tmp_path, "instagram:old", "https://scontent-icn2-1.cdninstagram.com/fresh.jpg")
    assert p2 and len(calls) == n + 1  # a rescan's new link is tried straight away
