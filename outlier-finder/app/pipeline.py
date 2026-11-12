"""Analyze one video: get the file → measure edits → transcribe → Claude breakdown."""
from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import media
from .analyzer import AnalysisError, ClaudeAnalyzer, FakeAnalyzer
from .config import Config
from .db import connect
from .models import Post
from .providers import source_for
from .queries import get_video
from .transcribe import FakeTranscriber, Transcript, WhisperTranscriber, script_metrics

log = logging.getLogger(__name__)
_whisper: dict[str, WhisperTranscriber] = {}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def frames_dir(cfg: Config, video_id: str) -> Path:
    return cfg.media_dir / "frames" / media.safe_name(video_id)


def queue(cfg: Config, video_id: str) -> None:
    with connect(cfg.db_path) as conn:
        conn.execute(
            """INSERT INTO analyses (video_id, status, step, error, created_at, updated_at)
               VALUES (?, 'queued', 'waiting', NULL, ?, ?)
               ON CONFLICT(video_id) DO UPDATE SET status='queued', step='waiting', error=NULL,
                   updated_at=excluded.updated_at""",
            (video_id, _now(), _now()))


def _set(cfg: Config, video_id: str, **cols) -> None:
    cols["updated_at"] = _now()
    sets = ", ".join(f"{k} = ?" for k in cols)
    with connect(cfg.db_path) as conn:
        conn.execute(f"UPDATE analyses SET {sets} WHERE video_id = ?", [*cols.values(), video_id])


def _post_from_row(v: dict) -> Post:
    return Post(id=v["id"], platform=v["platform"], creator_id=v["channel_id"], title=v["title"] or "",
                published_at=v["published_at"], duration_s=v["duration_s"] or 0, views=v["views"],
                url=v["url"], media_url=v["media_url"])


def get_video_file(cfg: Config, v: dict, workdir: Path, provider=None) -> Path:
    dest = workdir / "video.mp4"
    is_demo = "demo_" in v["id"] or (v.get("media_url") or "").startswith("demo://")
    if is_demo:
        return media.make_demo_clip(dest)
    if not cfg.analysis.allow_downloads:
        raise media.MediaError("Downloads are turned off (analysis.allow_downloads). Upload the file instead.")
    if v["platform"] == "youtube":
        return media.download_youtube(v["url"] or f"https://www.youtube.com/shorts/{v['id'].split(':', 1)[1]}", dest)
    if v.get("media_url"):
        try:
            return media.download(v["media_url"], dest)
        except media.MediaError:
            log.info("stored media URL failed for %s; asking the provider for a fresh one", v["id"])
    prov = provider or source_for(cfg, v["platform"]).provider
    fresh = prov.refresh_media_url(_post_from_row(v)) if prov else None
    if not fresh:
        raise media.MediaError("Couldn't get a download link for this video. Upload the file on this page instead.")
    return media.download(fresh, dest)


def run_analysis(cfg: Config, video_id: str, upload: Path | None = None,
                 transcriber=None, analyzer=None, provider=None) -> None:
    """Runs in a background thread; progress and errors are written to the analyses table."""
    workdir = cfg.media_dir / "work" / media.safe_name(video_id)
    try:
        v = get_video(cfg, video_id)
        if not v:
            raise AnalysisError("Video not found.")
        demo = "demo_" in video_id
        _set(cfg, video_id, status="running", step="getting the video file")
        workdir.mkdir(parents=True, exist_ok=True)
        path = upload or get_video_file(cfg, v, workdir, provider)

        _set(cfg, video_id, step="measuring edits")
        info = media.probe(path)
        duration = info["duration_s"] or float(v.get("duration_s") or 0)
        cuts = media.scene_cuts(path)
        edit = media.edit_metrics(duration, cuts)
        fdir = frames_dir(cfg, video_id)
        shutil.rmtree(fdir, ignore_errors=True)
        frames = media.extract_frames(path, media.frame_times(duration, cuts, cfg.analysis.max_frames), fdir)

        _set(cfg, video_id, step="transcribing")
        if info["has_audio"]:
            audio = media.extract_audio(path, workdir / "audio.wav")
            if transcriber is None:
                transcriber = FakeTranscriber() if demo else _whisper.setdefault(
                    cfg.analysis.whisper_model, WhisperTranscriber(cfg.analysis.whisper_model))
            transcript = transcriber.transcribe(audio)
        else:
            transcript = Transcript(None, [])
        script = script_metrics(transcript, duration)
        _set(cfg, video_id, transcript_json=json.dumps(transcript.to_dict()),
             metrics_json=json.dumps({"edit": edit, "script": script, "video": info}))

        _set(cfg, video_id, step="analyzing with Claude")
        if analyzer is None:
            analyzer = FakeAnalyzer() if (demo or not cfg.anthropic_ready) else ClaudeAnalyzer(
                cfg.analysis.model, cfg.analysis.effort)
        stats = {k: v.get(k) for k in ("platform", "title", "views", "likes", "comments", "shares", "saves",
                                        "followers", "channel_median", "outlier_score", "engagement_rate",
                                        "published_at")}
        stats["description"] = (v.get("description") or "")[:500]
        breakdown = analyzer.analyze(stats, edit, script, transcript.as_prompt_text(), frames)
        _set(cfg, video_id, status="done", step="done", breakdown_json=json.dumps(breakdown),
             model=breakdown.get("_meta", {}).get("model"), demo=1 if getattr(analyzer, "demo", False) else 0)
    except Exception as e:
        log.exception("analysis failed for %s", video_id)
        _set(cfg, video_id, status="failed", error=f"{e}" if isinstance(e, (AnalysisError, media.MediaError,
                                                                               RuntimeError)) else
             f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)  # keep frames, drop the video and audio
        if upload:
            upload.unlink(missing_ok=True)


def get_analysis(cfg: Config, video_id: str) -> dict | None:
    with connect(cfg.db_path) as conn:
        row = conn.execute("SELECT * FROM analyses WHERE video_id = ?", (video_id,)).fetchone()
    if not row:
        return None
    a = dict(row)
    for col, key in (("transcript_json", "transcript"), ("metrics_json", "metrics"),
                     ("breakdown_json", "breakdown")):
        a[key] = json.loads(a[col]) if a.get(col) else None
    a["frames"] = sorted(p.name for p in frames_dir(cfg, video_id).glob("*.jpg")) \
        if frames_dir(cfg, video_id).exists() else []
    return a
