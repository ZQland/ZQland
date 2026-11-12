"""Picks the data source for each platform from niches.yaml + available credentials."""
from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..models import PLATFORMS
from .fake import FakeProvider

_cache: dict[tuple, object] = {}


@dataclass
class SourceStatus:
    platform: str
    mode: str            # official | scraper | off
    provider: object | None
    demo: bool           # True when using synthetic data because credentials are missing
    note: str


def _cached(key: tuple, factory):
    if key not in _cache:
        _cache[key] = factory()
    return _cache[key]


def _demo(platform: str, can_search: bool = True) -> FakeProvider:
    def make():
        p = FakeProvider(platform)
        p.can_search = can_search
        return p
    return _cached(("fake", platform, can_search), make)


def source_for(cfg: Config, platform: str) -> SourceStatus:
    mode = cfg.platforms.get(platform, "off")
    if mode == "off":
        return SourceStatus(platform, mode, None, False, "Turned off in niches.yaml.")

    if platform == "youtube":
        if not cfg.youtube_api_key:
            return SourceStatus(platform, "official", _demo(platform), True,
                                "Add YOUTUBE_API_KEY to use real data.")
        from .youtube import YouTubeProvider
        return SourceStatus(platform, "official",
                            _cached(("yt", cfg.youtube_api_key), lambda: YouTubeProvider(cfg.youtube_api_key)),
                            False, "YouTube Data API (official).")

    if platform == "tiktok":
        if mode == "official":
            return SourceStatus(platform, mode, None, False,
                                "TikTok has no official API for other creators' videos. "
                                "Set tiktok: scraper to enable it.")
        if not cfg.apify_token:
            return SourceStatus(platform, mode, _demo(platform), True, "Add APIFY_TOKEN to use real data.")
        from .apify import TikTokScraperProvider
        return SourceStatus(platform, mode,
                            _cached(("tt", cfg.apify_token), lambda: TikTokScraperProvider(cfg.apify_token)),
                            False, "Apify TikTok scraper (third-party).")

    if platform == "instagram":
        if mode == "official":
            if not (cfg.ig_access_token and cfg.ig_user_id):
                return SourceStatus(platform, mode, _demo(platform, can_search=False), True,
                                    "Add IG_ACCESS_TOKEN and IG_USER_ID to use real data. "
                                    "Watchlist only: no keyword search.")
            from .instagram_official import InstagramOfficialProvider
            return SourceStatus(platform, mode, _cached(
                ("igo", cfg.ig_access_token),
                lambda: InstagramOfficialProvider(cfg.ig_access_token, cfg.ig_user_id)),
                False, "Instagram Business Discovery (official). Watchlist only.")
        if not cfg.apify_token:
            return SourceStatus(platform, mode, _demo(platform), True, "Add APIFY_TOKEN to use real data.")
        from .apify import InstagramScraperProvider
        return SourceStatus(platform, mode,
                            _cached(("igs", cfg.apify_token), lambda: InstagramScraperProvider(cfg.apify_token)),
                            False, "Apify Instagram scraper (third-party).")

    return SourceStatus(platform, "off", None, False, "Unknown platform.")


def all_sources(cfg: Config) -> list[SourceStatus]:
    return [source_for(cfg, p) for p in PLATFORMS]


def reset_cache() -> None:
    _cache.clear()
