"""FastAPI web app: outliers, opportunities, creators, analyses, scans."""
from __future__ import annotations

import shutil
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import pipeline, queries
from .analyzer import SCORE_CRITERIA
from .config import load_config
from .db import init_db
from .models import PLATFORM_LABELS, PLATFORMS, split_id
from .providers import all_sources, source_for
from .scanner import _finish, add_creator, refresh_creators, run_scan, start_scan

HERE = Path(__file__).resolve().parent
cfg = load_config()
init_db(cfg.db_path)

app = FastAPI(title="Outlier Finder")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


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
templates.env.globals.update(PLATFORM_LABELS=PLATFORM_LABELS, PLATFORMS=PLATFORMS,
                             SCORE_CRITERIA=SCORE_CRITERIA, split_id=split_id)


def _ctx(request: Request, **kw) -> dict:
    return {"request": request, "cfg": cfg, "genres": list(cfg.genres), "sources": all_sources(cfg),
            "summary": queries.summary(cfg), **kw}


def _running() -> bool:
    return any(s["status"] == "running" for s in queries.recent_scans(cfg, 5))


def _bg(fn, *args, **kwargs) -> None:
    threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True).start()


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
def feed(request: Request, platform: str = "", genre: str = "", keyword: str = "", sort: str = "outlier",
         min_outlier: float = 0, max_subs: int = 0):
    videos = queries.list_outliers(cfg, genre or None, keyword or None, min_outlier or None,
                                   max_subs or None, sort, platform=platform or None)
    return templates.TemplateResponse(request, "index.html", _ctx(
        request, videos=videos, platform=platform, genre=genre, keyword=keyword,
        keywords=cfg.genres.get(genre, []) if genre else [], sort=sort,
        min_outlier=min_outlier, max_subs=max_subs, running=_running()))


@app.get("/opportunities", response_class=HTMLResponse)
def opportunities(request: Request, genre: str = "", platform: str = ""):
    rows = queries.keyword_opportunities(cfg, genre or None, platform or None)
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
    if not a or a["status"] not in ("queued", "running"):
        pipeline.queue(cfg, video_id)
        _bg(pipeline.run_analysis, cfg, video_id)
    return RedirectResponse(f"/video/{video_id}#analysis", status_code=303)


@app.post("/upload/{video_id:path}")
async def upload(video_id: str, file: UploadFile = File(...)):
    if not queries.get_video(cfg, video_id):
        raise HTTPException(404, "Video not found")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".mp4", ".mov", ".m4v", ".webm", ".mkv"):
        raise HTTPException(400, "Upload an .mp4, .mov, .m4v, .webm or .mkv file.")
    dest = cfg.media_dir / "uploads" / f"{uuid.uuid4().hex}{suffix}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    pipeline.queue(cfg, video_id)
    _bg(pipeline.run_analysis, cfg, video_id, upload=dest)
    return RedirectResponse(f"/video/{video_id}#analysis", status_code=303)


@app.get("/frame/{name}")
def frame(name: str, v: str):
    path = pipeline.frames_dir(cfg, v) / Path(name).name
    if path.suffix != ".jpg" or not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg")


@app.get("/creators", response_class=HTMLResponse)
def creators(request: Request, platform: str = "", error: str = ""):
    return templates.TemplateResponse(request, "creators.html", _ctx(
        request, creators=queries.list_creators(cfg, platform or None), platform=platform,
        error=error, running=_running()))


def _add_creator_job(scan_id: int, platform: str, handle: str) -> None:
    try:
        cid = add_creator(cfg, platform, handle)
        _finish(cfg, scan_id, "finished", 0, f"Added {cid}.")
    except Exception as e:
        _finish(cfg, scan_id, "failed", 0, f"{type(e).__name__}: {e}")


@app.post("/creators")
def create_creator(platform: str = Form(...), handle: str = Form(...)):
    handle = handle.strip()
    if platform not in PLATFORMS or not handle:
        return RedirectResponse("/creators?error=Pick+a+platform+and+enter+a+handle+or+URL", status_code=303)
    if source_for(cfg, platform).provider is None:
        return RedirectResponse(f"/creators?error={platform}+has+no+data+source", status_code=303)
    scan_id = start_scan(cfg, f"add {platform} creator {handle}")
    _bg(_add_creator_job, scan_id, platform, handle)
    return RedirectResponse("/creators", status_code=303)


@app.post("/creators/refresh")
def refresh(channel_id: str = Form("")):
    if not _running():
        scan_id = start_scan(cfg, f"refresh {channel_id}" if channel_id else "refresh tracked creators")
        _bg(refresh_creators, cfg, scan_id, [channel_id] if channel_id else None)
    return RedirectResponse(f"/creator/{channel_id}" if channel_id else "/creators", status_code=303)


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
        conn.execute("UPDATE channels SET tracked = ? WHERE id = ?", (1 if tracked else 0, channel_id))
    return RedirectResponse(f"/creator/{channel_id}", status_code=303)


@app.get("/scans", response_class=HTMLResponse)
def scans(request: Request):
    per_kw = {"youtube": "~100 units", "tiktok": f"~{cfg.settings.results_per_keyword} results",
              "instagram": f"~{cfg.settings.results_per_keyword} results"}
    return templates.TemplateResponse(request, "scans.html", _ctx(
        request, scans=queries.recent_scans(cfg), per_kw=per_kw, running=_running()))


@app.post("/scans")
async def create_scan(request: Request):
    form = await request.form()
    target = form.get("target", "all")
    genres = list(cfg.genres) if target == "all" else [target]
    if any(g not in cfg.genres for g in genres):
        raise HTTPException(400, "Unknown genre")
    platforms = [p for p in form.getlist("platforms") if p in PLATFORMS] or \
        [s.platform for s in all_sources(cfg) if s.provider is not None and s.provider.can_search]
    if not _running():
        demo = [p for p in platforms if source_for(cfg, p).demo]
        label = f"{target} on {', '.join(platforms)}" + (f" (demo: {', '.join(demo)})" if demo else "")
        _bg(run_scan, cfg, start_scan(cfg, label), genres, platforms)
    return RedirectResponse("/scans", status_code=303)


# --- JSON API ----------------------------------------------------------------

@app.get("/api/outliers")
def api_outliers(platform: str = "", genre: str = "", keyword: str = "", sort: str = "outlier",
                 min_outlier: float = 0, max_subs: int = 0, limit: int = 100):
    return JSONResponse(queries.list_outliers(cfg, genre or None, keyword or None, min_outlier or None,
                                              max_subs or None, sort, limit=limit, platform=platform or None))


@app.get("/api/opportunities")
def api_opportunities(genre: str = "", platform: str = ""):
    return JSONResponse(queries.keyword_opportunities(cfg, genre or None, platform or None))


@app.get("/api/analysis/{video_id:path}")
def api_analysis(video_id: str):
    a = pipeline.get_analysis(cfg, video_id)
    if not a:
        raise HTTPException(404, "No analysis for this video")
    return JSONResponse({k: a[k] for k in ("video_id", "status", "step", "error", "model", "demo",
                                           "transcript", "metrics", "breakdown")})
