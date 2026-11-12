"""The interface every platform data source implements."""
from __future__ import annotations

from typing import Protocol

from ..models import Post, Profile


class ProviderUnavailable(RuntimeError):
    """Raised when a provider can't do what was asked (e.g. official TikTok search)."""


class Provider(Protocol):
    platform: str
    name: str                 # shown in the UI, e.g. "YouTube Data API"
    can_search: bool          # False for official Instagram (watchlist only)
    usage: int                # quota units / results / requests used this session

    def search(self, query: str, lookback_days: int, limit: int) -> tuple[list[Post], int | None]:
        """Top recent short-form posts for a keyword, plus an approximate supply count."""
        ...

    def profiles(self, creator_ids: list[str], sample_size: int,
                 follower_range: tuple[int, int] | None) -> list[Profile]:
        """Creator info + recent posts. Skip recent posts for creators outside follower_range."""
        ...

    def lookup_creator(self, handle_or_url: str, sample_size: int) -> Profile:
        """Resolve a handle/URL the user typed into a creator with recent posts."""
        ...

    def refresh_media_url(self, post: Post) -> str | None:
        """A fresh direct video URL for downloading, if this source can provide one."""
        ...
