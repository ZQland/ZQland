"""Video files: download, probe, scene-cut detection, frame sampling, audio extraction."""
from __future__ import annotations

import importlib.util
import json
import re
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


def download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "Mozilla/5.0 (outlier-finder research tool)"}
    with httpx.stream("GET", url, headers=headers, follow_redirects=True, timeout=120) as r:
        if r.status_code >= 400:
            raise MediaError(f"Download failed with HTTP {r.status_code} (the link may have expired).")
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
