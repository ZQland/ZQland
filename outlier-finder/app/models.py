"""Platform-neutral records that every data provider returns."""
from __future__ import annotations

from dataclasses import dataclass, field

PLATFORMS = ("youtube", "tiktok", "instagram")
PLATFORM_LABELS = {"youtube": "YouTube Shorts", "tiktok": "TikTok", "instagram": "Instagram Reels"}


def make_id(platform: str, native_id: str) -> str:
    return f"{platform}:{native_id}"


def split_id(full_id: str) -> tuple[str, str]:
    platform, _, native = full_id.partition(":")
    return platform, native


@dataclass
class Creator:
    id: str                    # "<platform>:<native id or username>"
    platform: str
    handle: str | None         # @username where the platform has one
    title: str | None
    followers: int | None      # None when hidden or unknown
    url: str | None = None


@dataclass
class Post:
    id: str                    # "<platform>:<native id>"
    platform: str
    creator_id: str
    title: str                 # title for YouTube, caption for TikTok/Instagram
    published_at: str | None   # ISO-8601 UTC
    duration_s: int
    views: int | None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    saves: int | None = None
    thumbnail: str | None = None
    url: str | None = None          # public page for people
    media_url: str | None = None    # direct video file (scrapers only; expires)
    description: str = ""
    creator_followers: int | None = None  # when the search result already includes it
    language: str | None = None           # e.g. "en", "hi" when the platform reports it


@dataclass
class Profile:
    """A creator plus their recent posts (None when we skipped fetching them)."""
    creator: Creator
    recent_posts: list[Post] | None = field(default=None)
