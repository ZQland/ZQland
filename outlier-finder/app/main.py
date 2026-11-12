"""FastAPI web app: outlier feed, keyword opportunities, scans."""
from __future__ import annotations

import threading
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import queries
from .config import load_config
from .db import init_db
from .demo import FakeYouTubeClient
from .scanner import run_scan, start_scan

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


templates.env.filters["num"] = _fmt


def _ctx(request: Request, **kw) -> dict:
    return {"request": request, "cfg": cfg, "genres": list(cfg.genres),
            "summary": queries.summary(cfg), **kw}


def _running() -> bool:
    return any(s["status"] == "running" for s in queries.recent_scans(cfg, 5))


@app.get("/", response_class=HTMLResponse)
def feed(request: Request, genre: str = "", keyword: str = "", sort: str = "outlier",
         min_outlier: float = 0, max_subs: int = 0):
    videos = queries.list_outliers(cfg, genre or None, keyword or None,
                                   min_outlier or None, max_subs or None, sort)
    keywords = cfg.genres.get(genre, []) if genre else []
    return templates.TemplateResponse(request, "index.html", _ctx(
        request, videos=videos, genre=genre, keyword=keyword, keywords=keywords, sort=sort,
        min_outlier=min_outlier, max_subs=max_subs, running=_running()))


@app.get("/opportunities", response_class=HTMLResponse)
def opportunities(request: Request, genre: str = ""):
    rows = queries.keyword_opportunities(cfg, genre or None)
    return templates.TemplateResponse(request, "opportunities.html",
                                      _ctx(request, rows=rows, genre=genre))


@app.get("/video/{video_id}", response_class=HTMLResponse)
def video(request: Request, video_id: str):
    v = queries.get_video(cfg, video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    return templates.TemplateResponse(request, "video.html", _ctx(request, v=v))


@app.get("/scans", response_class=HTMLResponse)
def scans(request: Request):
    est = {g: len(kws) * 100 for g, kws in cfg.genres.items()}
    return templates.TemplateResponse(request, "scans.html", _ctx(
        request, scans=queries.recent_scans(cfg), estimates=est, running=_running()))


@app.post("/scans")
def create_scan(target: str = Form(...)):
    if _running():
        return RedirectResponse("/scans", status_code=303)
    genres = list(cfg.genres) if target == "all" else [target]
    if any(g not in cfg.genres for g in genres):
        raise HTTPException(400, "Unknown genre")
    client = FakeYouTubeClient() if cfg.demo_mode else None
    label = f"{target} (demo data)" if cfg.demo_mode else target
    scan_id = start_scan(cfg, label)
    threading.Thread(target=run_scan, args=(cfg, scan_id, genres, client), daemon=True).start()
    return RedirectResponse("/scans", status_code=303)


@app.get("/api/outliers")
def api_outliers(genre: str = "", keyword: str = "", sort: str = "outlier",
                 min_outlier: float = 0, max_subs: int = 0, limit: int = 100):
    return JSONResponse(queries.list_outliers(cfg, genre or None, keyword or None,
                                              min_outlier or None, max_subs or None, sort,
                                              limit=limit))


@app.get("/api/opportunities")
def api_opportunities(genre: str = ""):
    return JSONResponse(queries.keyword_opportunities(cfg, genre or None))
