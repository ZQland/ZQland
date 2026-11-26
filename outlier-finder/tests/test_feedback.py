"""Every action reports back on the page: one-time toasts, and JSON status for live progress."""
from fastapi.testclient import TestClient

from app import pipeline, queries
from app.providers.fake import FakeProvider
from app.scanner import run_scan, start_scan

ALL = ["youtube", "tiktok", "instagram"]


def _web(cfg, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "cfg", cfg)
    return main, TestClient(main.app)


def test_action_shows_one_time_toast(cfg, monkeypatch):
    _, web = _web(cfg, monkeypatch)
    r = web.post("/creators/track", data={}, follow_redirects=False)
    assert r.status_code == 303 and "flash" in r.cookies

    page = web.get("/creators")
    assert "Select at least one creator to track." in page.text and 'class="toast error"' in page.text
    assert "flash" not in web.cookies  # consumed: a reload doesn't repeat it
    assert "Select at least one creator" not in web.get("/creators").text


def test_static_and_api_requests_dont_consume_the_toast(cfg, monkeypatch):
    _, web = _web(cfg, monkeypatch)
    web.post("/settings/wide-scan", data={"on": "1"}, follow_redirects=False)
    web.get("/static/app.js")
    assert "Wide scan on" in web.get("/scans").text


def test_busy_actions_report_errors(cfg, monkeypatch):
    main, web = _web(cfg, monkeypatch)
    sid = start_scan(cfg, "already running")
    r = web.post("/scans", data={"target": "all"})
    assert "A scan is already running" in r.text
    r = web.post("/creators/refresh", data={})
    assert "Another job is running" in r.text
    assert f'data-scan-job="{sid}"' in r.text  # header progress pill polls this scan

    r = web.post(f"/scans/{sid}/cancel")
    assert f"Scan #{sid} cancelled" in r.text
    assert f"Scan #{sid} had already stopped" in web.post(f"/scans/{sid}/cancel").text


def test_status_endpoints(cfg, monkeypatch):
    main, web = _web(cfg, monkeypatch)
    sid = start_scan(cfg, "all")
    run_scan(cfg, sid, list(cfg.genres), ALL, {p: FakeProvider(p) for p in ALL}, wide=True)
    s = web.get(f"/api/scans/{sid}").json()
    assert s["status"] == "finished" and s["id"] == sid
    assert web.get("/api/scans/99999").status_code == 404

    vid = queries.list_outliers(cfg, platform="tiktok")[0]["id"]
    assert web.get(f"/api/analysis-status/{vid}").json()["error"] == "No analysis for this video"
    pipeline.queue(cfg, vid)
    assert web.get(f"/api/analysis-status/{vid}").json() == {"status": "queued", "step": "waiting", "error": None}
    page = web.get(f"/video/{vid}")
    assert f'data-analysis-job="{vid}"' in page.text and "Queued" in page.text


def test_bad_upload_type_reports_on_the_video_page(cfg, monkeypatch):
    main, web = _web(cfg, monkeypatch)
    run_scan(cfg, start_scan(cfg, "all"), list(cfg.genres), ALL, {p: FakeProvider(p) for p in ALL}, wide=True)
    vid = queries.list_outliers(cfg, platform="tiktok")[0]["id"]
    r = web.post(f"/upload/{vid}", files={"file": ("notes.txt", b"hi", "text/plain")})
    assert r.url.path == f"/video/{vid}" and "Can&#39;t analyze “notes.txt”" in r.text
