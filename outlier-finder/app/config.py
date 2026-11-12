"""Loads settings from niches.yaml and environment variables (.env supported)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so the app runs without extra dependencies."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Settings:
    lookback_days: int = 90
    results_per_keyword: int = 50
    max_followers: int = 100_000
    min_followers: int = 100
    min_views: int = 10_000
    min_outlier_score: float = 3.0
    shorts_only: bool = True          # YouTube only: keep videos ≤ 180s
    channel_sample_size: int = 30
    channel_cache_days: int = 7


@dataclass
class AnalysisSettings:
    model: str = "claude-opus-5-5"
    effort: str = "high"
    whisper_model: str = "base"       # tiny / base / small / medium / large-v3
    max_frames: int = 8
    allow_downloads: bool = True      # download video files from scrapers / yt-dlp


@dataclass
class Config:
    settings: Settings
    genres: dict[str, list[str]] = field(default_factory=dict)
    platforms: dict[str, str] = field(default_factory=dict)   # platform -> official|scraper|off
    analysis: AnalysisSettings = field(default_factory=AnalysisSettings)
    youtube_api_key: str | None = None
    apify_token: str | None = None
    ig_access_token: str | None = None
    ig_user_id: str | None = None
    anthropic_ready: bool = False
    db_path: Path = ROOT / "data" / "outliers.db"
    media_dir: Path = ROOT / "data" / "media"

    @property
    def api_key(self) -> str | None:  # kept for older code paths
        return self.youtube_api_key


def _pick(cls, raw: dict, renames: dict[str, str] | None = None):
    raw = dict(raw or {})
    for old, new in (renames or {}).items():
        if old in raw and new not in raw:
            raw[new] = raw.pop(old)
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in raw.items() if k in names})


def load_config(niches_path: Path | None = None) -> Config:
    _load_dotenv(ROOT / ".env")
    raw = yaml.safe_load((niches_path or ROOT / "niches.yaml").read_text()) or {}
    settings = _pick(Settings, raw.get("settings"),
                     {"max_subscribers": "max_followers", "min_subscribers": "min_followers"})
    platforms = {"youtube": "official", "tiktok": "scraper", "instagram": "scraper"}
    platforms.update(raw.get("platforms") or {})
    env = os.environ.get
    data_dir = Path(env("OUTLIER_DATA_DIR", ROOT / "data"))
    return Config(
        settings=settings,
        genres={g: list(kws) for g, kws in (raw.get("genres") or {}).items()},
        platforms=platforms,
        analysis=_pick(AnalysisSettings, raw.get("analysis")),
        youtube_api_key=env("YOUTUBE_API_KEY") or None,
        apify_token=env("APIFY_TOKEN") or None,
        ig_access_token=env("IG_ACCESS_TOKEN") or None,
        ig_user_id=env("IG_USER_ID") or None,
        anthropic_ready=bool(env("ANTHROPIC_API_KEY") or env("ANTHROPIC_AUTH_TOKEN")),
        db_path=Path(env("OUTLIER_DB", data_dir / "outliers.db")),
        media_dir=data_dir / "media",
    )
