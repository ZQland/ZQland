"""Loads settings from niches.yaml and environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
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
    max_subscribers: int = 100_000
    min_subscribers: int = 100
    min_views: int = 10_000
    min_outlier_score: float = 3.0
    shorts_only: bool = True
    channel_sample_size: int = 30
    channel_cache_days: int = 7


@dataclass
class Config:
    settings: Settings
    genres: dict[str, list[str]] = field(default_factory=dict)
    api_key: str | None = None
    db_path: Path = ROOT / "data" / "outliers.db"

    @property
    def demo_mode(self) -> bool:
        return not self.api_key


def load_config(niches_path: Path | None = None) -> Config:
    _load_dotenv(ROOT / ".env")
    raw = yaml.safe_load((niches_path or ROOT / "niches.yaml").read_text())
    settings = Settings(**(raw.get("settings") or {}))
    genres = {g: list(kws) for g, kws in (raw.get("genres") or {}).items()}
    db_path = Path(os.environ.get("OUTLIER_DB", ROOT / "data" / "outliers.db"))
    return Config(
        settings=settings,
        genres=genres,
        api_key=os.environ.get("YOUTUBE_API_KEY") or None,
        db_path=db_path,
    )
