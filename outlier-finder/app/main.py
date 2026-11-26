"""FastAPI web app: outliers, opportunities, creators, analyses, scans."""
from __future__ import annotations

import json
import logging
import shutil
import threading
import uuid
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, unquote

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BeforeValidator

from . import media, pipeline, queries
from .analyzer import SCORE_CRITERIA
from .pipeline import ANALYSIS_STEPS
from .config import load_config
from .db import connect, init_db
from .models import PLATFORM_LABELS, PLATFORMS, split_id
from .providers import all_sources, source_for
from .budget import apify_paused, month_summary
from .envcheck import environment_problems
from .verdict import metric_checks, tone, verdict
from .scanner import (add_creators, cancel_scan, split_handles, refresh_creators, run_scan, set_wide_scan, start_scan,
                      wide_scan_enabled)

HERE = Path(__file__).resolve().parent
cfg = load_config()
init_db(cfg.db_path)

app = FastAPI(title="Outlier Finder")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
# The public landing page (a static site built with the taste skill); its assets resolve under /welcome/.
app.mount("/welcome", StaticFiles(directory=HERE / "static" / "landing", html=True), name="welcome")
templates = Jinja2Templates(directory=HERE / "templates")

# HTML forms submit empty number inputs as "" (e.g. ?min_outlier=&max_subs=); treat that as unset.
_blank_to_zero = BeforeValidator(lambda v: 0 if isinstance(v, str) and not v.strip() else v)
OptFloat = Annotated[float, _blank_to_zero]
OptInt = Annotated[int, _blank_to_zero]
# Multi-select filters arrive as repeated params (?platform=tiktok&platform=instagram).
# Old single-value links (?platform=) still work: blanks are dropped.
_drop_blanks = BeforeValidator(lambda v: [x for x in (v or []) if x and str(x).strip()])
Multi = Annotated[list[str], Query(), _drop_blanks]


def _keywords_for(genres: list[str]) -> list[str]:
    return [k for g in genres for k in cfg.genres.get(g, [])]


def _fmt(n) -> str:
    if n is None:
        return "–"
    n = float(n)
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= size:
            return f"{n / size:.1f}".rstrip("0").rstrip(".") + unit
    return f"{n:.0f}"


def _mmss(s) -> str:
    s = float(s or 0)
    return f"{int(s // 60)}:{s % 60:04.1f}"


templates.env.filters["num"] = _fmt
templates.env.filters["mmss"] = _mmss
# (value, label, group) tuples for the multiselect macro; labels default to the value with spaces.
templates.env.filters["pair"] = lambda v, labels=None: (v, (labels or {}).get(v, v.replace("_", " ")), None)
templates.env.globals.update(verdict=verdict, metric_checks=metric_checks, tone=tone)


def _asset_version() -> str:
    """Changes whenever a stylesheet or script changes, so browsers never use a stale cached copy."""
    static = Path(__file__).parent / "static"
    return str(int(max((static / f).stat().st_mtime for f in ("style.css", "app.js", "filters.js"))))


templates.env.globals["asset_v"] = _asset_version  # called per page, so CSS edits show without a restart
templates.env.globals.update(PLATFORM_LABELS=PLATFORM_LABELS, PLATFORMS=PLATFORMS,
                             SCORE_CRITERIA=SCORE_CRITERIA, split_id=split_id,
                             ANALYSIS_STEPS=ANALYSIS_STEPS)


def _ctx(request: Request, **kw) -> dict:
    from .config import env_path
    return {"request": request, "cfg": cfg, "genres": list(cfg.genres), "sources": all_sources(cfg),
            "summary": queries.summary(cfg), "env_path": env_path(), "env_exists": env_path().exists(),
            "problems": queries.recent_problems(cfg), "apify_paused": apify_paused(cfg), "budget": month_summary(cfg),
            "env_problems": environment_problems(), "flash": getattr(request.state, "flash", None),
            "job": _running_scan(),
            "return_to": request.url.path + (f"?{request.url.query}" if request.url.query else ""), **kw}


# --- feedback: every action reports back on the next page -------------------------------
# A POST redirects with a one-time message in a short-lived cookie; the next HTML page shows it
# as a toast and the cookie is cleared. Survives the redirect without putting text in the URL.

FLASH_COOKIE = "flash"


def _redirect(url: str, msg: str | None = None, kind: str = "ok") -> RedirectResponse:
    """kind: ok | info | error."""
    r = RedirectResponse(url if url.startswith("/") else "/", status_code=303)
    if msg:
        r.set_cookie(FLASH_COOKIE, quote(json.dumps({"msg": msg, "kind": kind})), max_age=60, path="/",
                     httponly=True, samesite="lax")
    return r


@app.middleware("http")
async def _flash_middleware(request: Request, call_next):
    raw = request.cookies.get(FLASH_COOKIE)
    request.state.flash = None
    if raw:
        try:
            f = json.loads(unquote(raw))
            request.state.flash = {"msg": str(f["msg"]), "kind": f.get("kind", "ok")}
        except (ValueError, KeyError, TypeError):
            pass
    response = await call_next(request)
    if raw and response.headers.get("content-type", "").startswith("text/html"):
        response.delete_cookie(FLASH_COOKIE, path="/")
    return response


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# --- errors: every failure shows up on the page, not just in the terminal -------------------

log = logging.getLogger("outlier_finder")


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith("/api/") or "application/json" in request.headers.get("accept", "")


def _error_page(request: Request, status: int, title: str, message: str, details: list[str] | None = None):
    if _wants_json(request):
        return JSONResponse({"error": message, "details": details or []}, status_code=status)
    try:
        ctx = _ctx(request, status=status, title=title, message=message, details=details or [])
    except Exception:  # noqa: BLE001 - the error page must render even if the database is the problem
        ctx = {"request": request, "cfg": cfg, "sources": [], "summary": {"videos": 0, "channels": 0, "tracked": 0},
               "problems": [], "apify_paused": None, "env_problems": (), "genres": [], "status": status, "title": title,
               "message": message, "details": details or [], "return_to": "/"}
    return templates.TemplateResponse(request, "error.html", ctx, status_code=status)


_FIELD_NAMES = {"min_outlier": "Min outlier", "max_subs": "Max followers", "platform": "Platform",
                "genre": "Genre", "keyword": "Keyword", "limit": "Limit", "handles": "Handles", "file": "File"}


@app.exception_handler(RequestValidationError)
async def _invalid_request(request: Request, exc: RequestValidationError):
    details = []
    for e in exc.errors():
        field = str(e.get("loc", ["", "?"])[-1])
        given = e.get("input")
        details.append(f"{_FIELD_NAMES.get(field, field)}: {e.get('msg', 'invalid value')}"
                       + (f" (got {given!r})" if isinstance(given, (str, int, float)) else ""))
    return _error_page(request, 422, "Some filter or form values aren't valid",
                       "Fix the values below and try again.", details)


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    titles = {404: "Not found", 400: "That request didn't work", 405: "Not allowed"}
    return _error_page(request, exc.status_code, titles.get(exc.status_code, f"Error {exc.status_code}"),
                       str(exc.detail) if exc.detail else "")


@app.exception_handler(Exception)
async def _crash(request: Request, exc: Exception):
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return _error_page(request, 500, "Something went wrong", f"{type(exc).__name__}: {exc}",
                       ["The full traceback is in the terminal running the server."])


@app.post("/alerts/dismiss")
def dismiss_alerts(back: str = Form("/")):
    queries.dismiss_problems(cfg)
    return _redirect(back, "Problems dismissed.", "info")


def _running_scan() -> dict | None:
    return next((s for s in queries.recent_scans(cfg, 5) if s["status"] == "running"), None)


def _running() -> bool:
    return _running_scan() is not None


def _bg(fn, *args, **kwargs) -> None:
    def run():
        try:
            fn(*args, **kwargs)
        finally:  # new posts' thumbnail links expire in days: save them now
            try:
                media.cache_missing_thumbnails(cfg.db_path, cfg.media_dir)
            except Exception:  # noqa: BLE001
                log.exception("thumbnail caching failed")
    threading.Thread(target=run, daemon=True).start()


@app.get("/thumb/{video_id:path}")
def thumbnail(video_id: str):
    """A video's thumbnail from the local copy (fetched once; the platforms' links expire)."""
    with connect(cfg.db_path) as conn:
        row = conn.execute("SELECT thumbnail FROM videos WHERE id = ?", (video_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Video not found")
    path = media.cache_thumbnail(cfg.media_dir, video_id, row["thumbnail"])
    if not path:
        raise HTTPException(404, "Thumbnail unavailable")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})


def _sparkline(history: list[tuple[str, int]], w: int = 600, h: int = 120) -> dict | None:
    if len(history) < 2:
        return None
    vals = [f for _, f in history]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    pts = " ".join(f"{i / (len(vals) - 1) * w:.1f},{h - (f - lo) / span * (h - 10) - 5:.1f}"
                   for i, f in enumerate(vals))
    return {"points": pts, "w": w, "h": h, "lo": lo, "hi": hi,
            "start": history[0][0][:10], "end": history[-1][0][:10]}


# --- pages ------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def feed(request: Request, platform: Multi = [], genre: Multi = [], keyword: Multi = [], sort: str = "outlier",
         min_outlier: OptFloat = 0, max_subs: OptInt = 0):
    # Keywords only make sense inside the chosen genres; drop ones left over from a deselected genre.
    keyword = [k for k in keyword if not genre or k in _keywords_for(genre)]
    videos = queries.list_outliers(cfg, genre, keyword, min_outlier or None,
                                   max_subs or None, sort, platform=platform)
    return templates.TemplateResponse(request, "index.html", _ctx(
        request, videos=videos, platform=platform, genre=genre, keyword=keyword, sort=sort,
        keyword_options=[(k, k, g) for g, ks in cfg.genres.items() for k in ks],
        min_outlier=min_outlier, max_subs=max_subs, running=_running()))


@app.get("/breakdowns", response_class=HTMLResponse)
def breakdowns(request: Request, sort: str = "newest"):
    return templates.TemplateResponse(request, "breakdowns.html",
                                      _ctx(request, rows=queries.list_breakdowns(cfg, sort), sort=sort))


@app.get("/opportunities", response_class=HTMLResponse)
def opportunities(request: Request, genre: Multi = [], platform: Multi = []):
    rows = queries.keyword_opportunities(cfg, genre, platform)
    return templates.TemplateResponse(request, "opportunities.html",
                                      _ctx(request, rows=rows, genre=genre, platform=platform))


@app.get("/video/{video_id:path}", response_class=HTMLResponse)
def video(request: Request, video_id: str):
    v = queries.get_video(cfg, video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    analysis = pipeline.get_analysis(cfg, video_id)
    return templates.TemplateResponse(request, "video.html", _ctx(
        request, v=v, gained=queries.followers_gained(cfg, v), analysis=analysis,
        claude_ready=cfg.anthropic_ready))


@app.post("/analyze/{video_id:path}")
def analyze(video_id: str):
    if not queries.get_video(cfg, video_id):
        raise HTTPException(404, "Video not found")
    a = pipeline.get_analysis(cfg, video_id)
    if a and a["status"] in ("queued", "running"):
        return _redirect(f"/video/{video_id}#analysis", "This video is already being analyzed.", "info")
    pipeline.queue(cfg, video_id)
    _bg(pipeline.run_analysis, cfg, video_id)
    return _redirect(f"/video/{video_id}#analysis", "Analysis started. This page updates as each step finishes.")


@app.post("/upload/{video_id:path}")
async def upload(video_id: str, file: UploadFile = File(...)):
    if not queries.get_video(cfg, video_id):
        raise HTTPException(404, "Video not found")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".mp4", ".mov", ".m4v", ".webm", ".mkv"):
        return _redirect(f"/video/{video_id}#analysis",
                         f"Can't analyze “{file.filename or 'that file'}”: upload an .mp4, .mov, .m4v, .webm or .mkv video.",
                         "error")
    a = pipeline.get_analysis(cfg, video_id)
    if a and a["status"] in ("queued", "running"):
        return _redirect(f"/video/{video_id}#analysis", "This video is already being analyzed.", "info")
    dest = cfg.media_dir / "uploads" / f"{uuid.uuid4().hex}{suffix}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    pipeline.queue(cfg, video_id)
    _bg(pipeline.run_analysis, cfg, video_id, upload=dest)
    return _redirect(f"/video/{video_id}#analysis", f"Uploaded {file.filename}. Analysis started.")


@app.get("/frame/{name}")
def frame(name: str, v: str):
    path = pipeline.frames_dir(cfg, v) / Path(name).name
    if path.suffix != ".jpg" or not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg")


@app.get("/creators", response_class=HTMLResponse)
def creators(request: Request, platform: Multi = [], genre: Multi = [], error: str = ""):
    return templates.TemplateResponse(request, "creators.html", _ctx(
        request, creators=queries.list_creators(cfg, platform, genre=genre),
        suggestions=queries.suggested_creators(cfg, platform, genre),
        platform=platform, genre=genre, error=error, running=_running()))


@app.post("/creators")
def create_creators(platform: str = Form(...), handles: str = Form(""), handle: str = Form(""), genre: str = Form("")):
    """Track one or many creators: handles or profile URLs separated by new lines, spaces, commas, ; or |."""
    entries = split_handles(handles or handle)
    if platform not in PLATFORMS or not entries:
        return _redirect("/creators", "Pick a platform and enter at least one handle or profile URL.", "error")
    if genre and genre not in cfg.genres:
        raise HTTPException(400, "Unknown genre")
    if _running():
        return _redirect("/creators", "Another job is running. Wait for it to finish, then add the creators again.",
                         "error")
    scan_id = start_scan(cfg, f"add {len(entries)} creator{'s' if len(entries) > 1 else ''}"
                              f"{' to ' + genre if genre else ''}")
    _bg(add_creators, cfg, scan_id, entries, platform, genre or None)
    return _redirect("/creators", f"Looking up {_plural(len(entries), 'creator')}. Progress shows at the top of the page.")


@app.post("/creators/track")
def track_many(channel_id: Annotated[list[str], Form()] = [], genre: str = Form("")):
    """Track suggested creators. Free: their posts and medians are already stored."""
    from .db import connect
    if not channel_id:
        return _redirect("/creators", "Select at least one creator to track.", "error")
    with connect(cfg.db_path) as conn:
        marks = ",".join("?" * len(channel_id))
        conn.execute(f"UPDATE channels SET tracked = 1 WHERE id IN ({marks})", channel_id)
        if genre in cfg.genres:
            conn.execute(f"UPDATE channels SET genre = ? WHERE id IN ({marks}) AND genre IS NULL",
                         [genre, *channel_id])
    return _redirect("/creators", f"Now tracking {_plural(len(channel_id), 'more creator')}. Scans will check them.")


@app.post("/creator/{channel_id:path}/genre")
def set_genre(channel_id: str, genre: str = Form(""), back: str = Form("")):
    from .db import connect
    if genre and genre not in cfg.genres:
        raise HTTPException(400, "Unknown genre")
    with connect(cfg.db_path) as conn:
        found = conn.execute("UPDATE channels SET genre = ? WHERE id = ?", (genre or None, channel_id)).rowcount
    if not found:
        raise HTTPException(404, "Creator not found")
    return _redirect(back if back.startswith("/") else f"/creator/{channel_id}",
                     f"Genre set to {genre.replace('_', ' ')}." if genre else "Genre cleared.")


@app.post("/creators/refresh")
def refresh(channel_id: str = Form("")):
    back = f"/creator/{channel_id}" if channel_id else "/creators"
    if _running():
        return _redirect(back, "Another job is running. Try again when it finishes.", "error")
    scan_id = start_scan(cfg, f"refresh {channel_id}" if channel_id else "refresh tracked creators")
    _bg(refresh_creators, cfg, scan_id, [channel_id] if channel_id else None)
    return _redirect(back, "Checking for new posts. Progress shows at the top of the page.")


@app.get("/creator/{channel_id:path}", response_class=HTMLResponse)
def creator(request: Request, channel_id: str, sort: str = "outlier"):
    c = queries.get_creator(cfg, channel_id)
    if not c:
        raise HTTPException(404, "Creator not found")
    history = queries.follower_history(cfg, channel_id)
    return templates.TemplateResponse(request, "creator.html", _ctx(
        request, c=c, posts=queries.creator_posts(cfg, channel_id, sort), sort=sort,
        spark=_sparkline(history), snapshots=len(history), running=_running()))


@app.post("/track/{channel_id:path}")
def track(channel_id: str, tracked: int = Form(1)):
    from .db import connect
    with connect(cfg.db_path) as conn:
        found = conn.execute("UPDATE channels SET tracked = ? WHERE id = ?",
                             (1 if tracked else 0, channel_id)).rowcount
    if not found:
        raise HTTPException(404, "Creator not found")
    return _redirect(f"/creator/{channel_id}", "Tracking this creator. Scans will check them." if tracked
                     else "Stopped tracking this creator.")


@app.get("/scans", response_class=HTMLResponse)
def scans(request: Request):
    return templates.TemplateResponse(request, "scans.html", _ctx(
        request, scans=queries.recent_scans(cfg), running=_running(), wide=wide_scan_enabled(cfg),
        apify=month_summary(cfg) if cfg.apify_token else None, counts=queries.tracked_counts(cfg)))


@app.post("/scans/{scan_id}/cancel")
def cancel(scan_id: int):
    if cancel_scan(cfg, scan_id):
        return _redirect("/scans", f"Scan #{scan_id} cancelled. Results found so far are kept.", "info")
    return _redirect("/scans", f"Scan #{scan_id} had already stopped.", "info")


@app.post("/settings/wide-scan")
def toggle_wide_scan(on: str = Form("0")):
    if _running():
        return _redirect("/scans", "Wait for the running scan to finish before switching modes.", "error")
    set_wide_scan(cfg, on == "1")
    return _redirect("/scans", "Wide scan on: scans also search keywords for new creators." if on == "1"
                     else "Wide scan off: scans check your creator list only.", "info")


@app.post("/scans")
async def create_scan(request: Request):
    form = await request.form()
    target = form.get("target", "all")
    if target != "all" and target not in cfg.genres:
        raise HTTPException(400, "Unknown genre")
    genres = None if target == "all" else [target]  # all = every tracked creator, with or without a genre
    platforms = [p for p in form.getlist("platforms") if p in PLATFORMS] or \
        [s.platform for s in all_sources(cfg) if s.provider is not None]
    if not platforms:
        return _redirect("/scans", "Turn on at least one platform to scan.", "error")
    if _running():
        return _redirect("/scans", "A scan is already running. Wait for it or cancel it first.", "error")
    wide = wide_scan_enabled(cfg)
    demo = [p for p in platforms if source_for(cfg, p).demo]
    label = (f"{'wide' if wide else 'creators'} · {target} on {', '.join(platforms)}"
             + (f" (demo: {', '.join(demo)})" if demo else ""))
    _bg(run_scan, cfg, start_scan(cfg, label), genres, platforms, None, wide)
    return _redirect("/scans", f"Scan started: {target.replace('_', ' ')} on "
                               f"{', '.join(PLATFORM_LABELS[p] for p in platforms)}.")


# --- JSON API ----------------------------------------------------------------

@app.get("/api/outliers")
def api_outliers(platform: Multi = [], genre: Multi = [], keyword: Multi = [], sort: str = "outlier",
                 min_outlier: OptFloat = 0, max_subs: OptInt = 0, limit: int = 100):
    return JSONResponse(queries.list_outliers(cfg, genre, keyword, min_outlier or None,
                                              max_subs or None, sort, limit=limit, platform=platform))


@app.get("/api/opportunities")
def api_opportunities(genre: Multi = [], platform: Multi = []):
    return JSONResponse(queries.keyword_opportunities(cfg, genre, platform))


@app.get("/api/analysis-status/{video_id:path}")
def api_analysis_status(video_id: str):
    """Small payload for the progress poller on the video page."""
    a = pipeline.get_analysis(cfg, video_id)
    if not a:
        raise HTTPException(404, "No analysis for this video")
    return JSONResponse({k: a[k] for k in ("status", "step", "error")})


@app.get("/api/scans/{scan_id}")
def api_scan(scan_id: int):
    s = next((s for s in queries.recent_scans(cfg, 50) if s["id"] == scan_id), None)
    if not s:
        raise HTTPException(404, "Scan not found")
    return JSONResponse({k: s.get(k) for k in ("id", "status", "target", "message", "quota_used", "cost_usd")})


@app.get("/api/analysis/{video_id:path}")
def api_analysis(video_id: str):
    a = pipeline.get_analysis(cfg, video_id)
    if not a:
        raise HTTPException(404, "No analysis for this video")
    return JSONResponse({k: a[k] for k in ("video_id", "status", "step", "error", "model", "demo",
                                           "transcript", "metrics", "breakdown")})
