"""Video files: download, probe, scene-cut detection, frame sampling, audio extraction."""
from __future__ import annotations

import importlib.util
import json
import re
import time
import shutil
import subprocess
import sys
from pathlib import Path

import httpx

MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024
SCENE_THRESHOLD = 0.3   # 0–1; lower catches subtler cuts but also camera motion


class MediaError(RuntimeError):
    pass


def _run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)
    except FileNotFoundError as e:
        raise MediaError(f"{cmd[0]} is not installed. Install ffmpeg (it includes ffprobe).") from e
    except subprocess.CalledProcessError as e:
        raise MediaError(f"{Path(cmd[0]).name} failed: {(e.stderr or '')[-400:]}") from e


def safe_name(video_id: str) -> str:
    return re.sub(r"[^\w.-]", "_", video_id)


def download(url: str, dest: Path, auth_token: str | None = None) -> Path:
    """Fetch a video file. `auth_token` is sent only to Apify's API, whose storage links are private."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "Mozilla/5.0 (outlier-finder research tool)"}
    private = httpx.URL(url).host == "api.apify.com"
    if private and auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    try:
        try:
            return _stream_to(url, dest, headers, private)
        except httpx.ConnectError:
            # Instagram's links name a regional edge server (e.g. instagram.ftpa1-2.fna.fbcdn.net) picked for
            # where the scraper ran; some don't resolve elsewhere. The link is signed, not tied to the host,
            # so the same file is served by Meta's global host.
            alt = _global_meta_host(url)
            if not alt:
                raise
            return _stream_to(alt, dest, headers, False)
    except httpx.TransportError as e:  # DNS, refused, timeout: say what happened instead of a raw socket error
        host = httpx.URL(url).host
        msg = str(e)
        if isinstance(e, httpx.ConnectError) and ("nodename" in msg or "not known" in msg or "getaddrinfo" in msg):
            why = (f"this computer can't look up {host}. Instagram/TikTok links point at a regional video server "
                   "chosen for wherever the scraper ran, and some of them don't exist outside that region")
        elif isinstance(e, httpx.TimeoutException):
            why = f"{host} didn't answer in time"
        else:
            why = f"couldn't connect to {host} ({type(e).__name__})"
        raise MediaError(f"Download failed: {why}.") from e


def _global_meta_host(url: str) -> str | None:
    u = httpx.URL(url)
    host = u.host or ""
    if host != "scontent.cdninstagram.com" and (host.endswith(".fbcdn.net") or host.endswith(".cdninstagram.com")):
        return str(u.copy_with(host="scontent.cdninstagram.com"))
    return None


def _stream_to(url: str, dest: Path, headers: dict, private: bool) -> Path:
    with httpx.stream("GET", url, headers=headers, follow_redirects=True, timeout=120) as r:
        if r.status_code >= 400:
            if private and r.status_code in (401, 403, 404):
                why = ("Apify storage refused it: the file is private and needs APIFY_TOKEN, or it's past "
                       "Apify's 7-day retention")
            elif r.status_code in (401, 403, 410):
                why = "the platform's video links expire after a few hours"
            else:
                why = "the server returned an error"
            raise MediaError(f"Download failed with HTTP {r.status_code} ({why}).")
        size = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes():
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise MediaError("Video file is larger than 300 MB.")
                f.write(chunk)
    return dest


def ytdlp_available() -> bool:
    return importlib.util.find_spec("yt_dlp") is not None or shutil.which("yt-dlp") is not None


def download_youtube(url: str, dest: Path) -> Path:
    """Uses yt-dlp. Downloading from YouTube is against its terms of service:
    keep it to personal research, or upload the file on the video page instead."""
    if not ytdlp_available():
        raise MediaError("YouTube videos need yt-dlp to download (pip install yt-dlp), "
                         "or upload the video file on this page.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "yt_dlp"] if importlib.util.find_spec("yt_dlp") else ["yt-dlp"]
    _run(cmd + ["-f", "best[height<=720][ext=mp4]/best[height<=720]/best", "--no-playlist",
                "-o", str(dest), url], timeout=600)
    if not dest.exists():
        raise MediaError("yt-dlp finished but no file was written.")
    return dest


def make_demo_clip(dest: Path, seconds: int = 24) -> Path:
    """A synthetic vertical clip with hard cuts and a tone, for demo mode and tests."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    shots = [1.2, 1.8, 2.5, 3.0, 2.2, 4.0, 3.3, 2.0, 4.0]
    colors = ["0xE4572E", "0x29335C", "0xF3A712", "0x669BBC", "0xA8C686",
              "0x8E5572", "0x2E933C", "0xEFEFEF", "0x111111"]
    inputs, labels = [], []
    for i, (d, c) in enumerate(zip(shots, colors)):
        inputs += ["-f", "lavfi", "-i", f"color=c={c}:s=360x640:r=30:d={d}"]
        labels.append(f"[{i}:v]")
    n = len(shots)
    inputs += ["-f", "lavfi", "-i", f"sine=frequency=330:duration={sum(shots)}"]
    _run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex",
          f"{''.join(labels)}concat=n={n}:v=1:a=0[v]", "-map", "[v]", "-map", f"{n}:a",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(dest)])
    return dest


def probe(path: Path) -> dict:
    out = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    data = json.loads(out.stdout)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    return {
        "duration_s": round(float(data.get("format", {}).get("duration") or 0), 2),
        "width": video.get("width"), "height": video.get("height"),
        "has_audio": any(s.get("codec_type") == "audio" for s in data.get("streams", [])),
    }


def scene_cuts(path: Path, threshold: float = SCENE_THRESHOLD) -> list[float]:
    """Timestamps (s) where the picture changes sharply: hard cuts, mostly."""
    out = _run(["ffmpeg", "-hide_banner", "-i", str(path), "-vf",
                f"select='gt(scene,{threshold})',showinfo", "-an", "-f", "null", "-"], timeout=600)
    times = [float(m) for m in re.findall(r"pts_time:([\d.]+)", out.stderr)]
    return sorted({round(t, 2) for t in times if t > 0.1})


def edit_metrics(duration: float, cuts: list[float]) -> dict:
    duration = max(duration, 0.1)
    bounds = [0.0, *cuts, duration]
    shots = [b - a for a, b in zip(bounds, bounds[1:]) if b > a]
    return {
        "duration_s": round(duration, 1),
        "cuts": len(cuts),
        "cuts_per_10s": round(len(cuts) / duration * 10, 2),
        "avg_shot_s": round(sum(shots) / len(shots), 2) if shots else round(duration, 2),
        "longest_shot_s": round(max(shots), 2) if shots else round(duration, 2),
        "cuts_in_first_3s": sum(1 for c in cuts if c <= 3),
        "cut_times_s": cuts[:200],
    }


def frame_times(duration: float, cuts: list[float], max_frames: int) -> list[float]:
    """Hook frames first, then frames just after cuts, then evenly spaced fill."""
    picks = [t for t in (0.3, 1.5, 3.0) if t < duration]
    after_cuts = [c + 0.2 for c in cuts if 3.5 < c + 0.2 < duration]
    room = max(max_frames - len(picks), 0)
    if after_cuts and room:
        step = max(len(after_cuts) / room, 1)
        picks += [after_cuts[int(i * step)] for i in range(min(room, len(after_cuts)))]
    k = 1
    while len(picks) < max_frames and duration > 4 and k < 50:
        t = round(duration * k / (max_frames + 1), 1)
        if all(abs(t - p) > 1.0 for p in picks):
            picks.append(t)
        k += 1
    return sorted(round(t, 1) for t in picks[:max_frames])


def extract_frames(path: Path, times: list[float], out_dir: Path, width: int = 512) -> list[tuple[float, Path]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for t in times:
        dest = out_dir / f"{t:07.2f}.jpg"
        _run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t}", "-i", str(path), "-frames:v", "1",
              "-vf", f"scale={width}:-2", "-q:v", "4", str(dest)])
        if dest.exists():
            frames.append((t, dest))
    return frames


def extract_audio(path: Path, dest: Path) -> Path:
    _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", str(dest)])
    return dest


# --- thumbnails ------------------------------------------------------------------------
# Instagram/TikTok thumbnail links are signed and expire after a few days, so a feed of older
# posts turns into broken images. Each thumbnail is saved locally the first time it's fetched
# (right after a scan, or on first view) and served from disk after that.
THUMB_MAX_BYTES = 3 * 1024 * 1024
_MISS_RETRY_SECONDS = 6 * 3600


def thumb_path(media_dir: Path, video_id: str) -> Path:
    return media_dir / "thumbs" / f"{safe_name(video_id)}.jpg"


def cache_thumbnail(media_dir: Path, video_id: str, url: str | None) -> Path | None:
    """The local copy of a video's thumbnail, fetching it once if needed. None if unavailable."""
    path = thumb_path(media_dir, video_id)
    if path.exists():
        return path
    if not url or not url.startswith(("http://", "https://")):
        return None
    miss = path.with_suffix(".miss")
    if miss.exists() and time.time() - miss.stat().st_mtime < _MISS_RETRY_SECONDS \
            and miss.read_text(errors="ignore") == url:  # a rescan brings a new link: try that at once
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    for candidate in filter(None, [url, _global_meta_host(url)]):
        try:
            r = httpx.get(candidate, timeout=8, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0 (outlier-finder research tool)"})
        except httpx.HTTPError:
            continue
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/") \
                and 0 < len(r.content) <= THUMB_MAX_BYTES:
            tmp = path.with_suffix(".part")
            tmp.write_bytes(r.content)
            tmp.replace(path)
            miss.unlink(missing_ok=True)
            return path
    miss.write_text(url)  # expired or unreachable: don't retry this link on every page view
    return None


def cache_missing_thumbnails(db_path: Path, media_dir: Path, limit: int = 1000) -> int:
    """Save every thumbnail not on disk yet, while the links still work. Returns how many were saved."""
    from concurrent.futures import ThreadPoolExecutor
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT id, thumbnail FROM videos WHERE thumbnail LIKE 'http%' "
                            "ORDER BY published_at DESC LIMIT ?", (limit,)).fetchall()
    todo = [(vid, url) for vid, url in rows if not thumb_path(media_dir, vid).exists()]
    if not todo:
        return 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        return sum(1 for p in pool.map(lambda r: cache_thumbnail(media_dir, *r), todo) if p)
