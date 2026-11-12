"""`python -m app.cli doctor`: explains why a platform shows "demo data", and tests each key.

Prints only masked key info (length and last 4 characters), never a full key.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import httpx

from .config import ROOT, _ant_profile_exists, env_path, load_config
from .providers import all_sources

KEYS = {
    "YOUTUBE_API_KEY": "YouTube Shorts",
    "APIFY_TOKEN": "TikTok + Instagram (scraper)",
    "ANTHROPIC_API_KEY": "Claude breakdowns",
    "IG_ACCESS_TOKEN": "Instagram official mode only",
    "IG_USER_ID": "Instagram official mode only",
}
OK, WARN, BAD = "✅", "⚠️ ", "❌"


def _mask(value: str) -> str:
    return f"{len(value)} chars, ends …{value[-4:]}" if len(value) > 8 else f"{len(value)} chars"


def _env_file_checks(out: list[str]) -> None:
    ENV_PATH = env_path()
    out.append(f"\n.env file\n  The app reads: {ENV_PATH}")
    if ENV_PATH.exists():
        out.append(f"  {OK} found")
    else:
        out.append(f"  {BAD} not found. Create it: cd {ROOT} && cp .env.example .env, then paste your keys")
    for wrong in (ROOT / ".env.txt", ROOT / ".env.example.txt", ROOT.parent / ".env", Path.cwd() / ".env"):
        if wrong.exists() and wrong.resolve() != ENV_PATH.resolve():
            out.append(f"  {WARN} also found {wrong}. The app ignores that file"
                       + (" (Windows added .txt: rename it to .env)" if wrong.suffix == ".txt" else ""))
    example = ROOT / ".env.example"
    if example.exists():
        filled = [l.split("=", 1)[0] for l in example.read_text(encoding="utf-8-sig").splitlines()
                  if "=" in l and not l.lstrip().startswith("#") and l.split("=", 1)[1].strip()]
        if filled:
            out.append(f"  {WARN} .env.example has values for {', '.join(filled)}. The app ignores .env.example: "
                       "move them into .env")


def _key_checks(out: list[str], shell_before: dict[str, str]) -> None:
    out.append("\nKeys the app sees")
    for key, purpose in KEYS.items():
        value = os.environ.get(key, "")
        if value:
            where = "terminal environment" if shell_before.get(key) else ".env"
            out.append(f"  {OK} {key:18} from {where} ({_mask(value)}) · {purpose}")
        elif key == "ANTHROPIC_API_KEY" and _ant_profile_exists():
            out.append(f"  {OK} {key:18} not set, but you're signed in with `ant auth login` · {purpose}")
        else:
            note = " (set but EMPTY in your terminal)" if key in shell_before else ""
            out.append(f"  {'  ' if 'IG_' in key else BAD} {key:18} missing{note} · {purpose}")


def _live_checks(out: list[str], cfg) -> None:
    out.append("\nLive checks")
    http = httpx.Client(timeout=20)
    if cfg.youtube_api_key:
        try:
            r = http.get("https://www.googleapis.com/youtube/v3/videos", params={"part": "id", "id": "dQw4w9WgXcQ"},
                         headers={"X-Goog-Api-Key": cfg.youtube_api_key})
            msg = r.json().get("error", {}).get("message", "") if r.status_code != 200 else ""
            out.append(f"  {OK} YouTube API key works" if r.status_code == 200 else
                       f"  {BAD} YouTube rejected the key ({r.status_code}): {msg}. Check that YouTube Data API v3 "
                       "is enabled for the key's Google Cloud project")
        except httpx.HTTPError as e:
            out.append(f"  {BAD} couldn't reach YouTube: {e}")
    if cfg.apify_token:
        try:
            r = http.get("https://api.apify.com/v2/users/me", headers={"Authorization": f"Bearer {cfg.apify_token}"})
            plan = (r.json().get("data", {}).get("plan") or {}).get("id") if r.status_code == 200 else None
            out.append(f"  {OK} Apify token works (plan: {plan})" if r.status_code == 200 else
                       f"  {BAD} Apify rejected the token ({r.status_code}). Copy it again from Settings → Integrations")
        except httpx.HTTPError as e:
            out.append(f"  {BAD} couldn't reach Apify: {e}")
    if cfg.anthropic_ready:
        try:
            import anthropic
            m = anthropic.Anthropic().models.retrieve(cfg.analysis.model)
            out.append(f"  {OK} Claude works ({m.display_name})")
        except Exception as e:  # report any failure in plain words
            out.append(f"  {BAD} Claude check failed: {type(e).__name__}: {getattr(e, 'message', e)}")
    else:
        out.append(f"  {BAD} Claude: no ANTHROPIC_API_KEY and no `ant auth login`, so analyses use the demo analyzer")


def _tool_checks(out: list[str], cfg) -> None:
    out.append("\nTools")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        ver = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True).stdout.split("\n")[0]
        out.append(f"  {OK} {ver[:60]}")
    else:
        out.append(f"  {BAD} ffmpeg not found on PATH: needed to analyze videos (brew/apt/winget install ffmpeg)")
    try:
        import faster_whisper  # noqa: F401
        size = cfg.analysis.whisper_model
        cached = any(Path.home().glob(f".cache/huggingface/hub/models--Systran--faster-whisper-{size}"))
        out.append(f"  {OK} faster-whisper installed · '{size}' model "
                   + ("downloaded" if cached else "downloads on first analysis (~150 MB)"))
    except ImportError:
        out.append(f"  {BAD} faster-whisper not installed: pip install -r requirements.txt")


def run() -> str:
    shell_before = {k: os.environ[k] for k in KEYS if k in os.environ}
    cfg = load_config()  # loads .env into the environment
    out = ["Outlier Finder doctor"]
    _env_file_checks(out)
    _key_checks(out, shell_before)
    out.append("\nData sources")
    for s in all_sources(cfg):
        state = "off" if not s.provider else ("DEMO DATA" if s.demo else f"live ({s.mode})")
        out.append(f"  {OK if s.provider and not s.demo else WARN} {s.platform:9} {state:14} {s.note}")
    _live_checks(out, cfg)
    _tool_checks(out, cfg)
    out.append("\nChanged .env? Stop the server (Ctrl+C) and start it again: --reload doesn't watch .env.")
    return "\n".join(out)
