"""YouTube Data API v3: official, free quota of 10,000 units/day."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import httpx

from .. import scoring
from ..models import Creator, Post, Profile, make_id, split_id
from .base import ProviderUnavailable

API = "https://www.googleapis.com/youtube/v3"
COST = {"search": 100, "videos": 1, "channels": 1, "playlistItems": 1}


class QuotaExceeded(RuntimeError):
    pass


class YouTubeClient:
    """Thin HTTP client with quota tracking."""

    def __init__(self, api_key: str, http: httpx.Client | None = None):
        self.api_key = api_key
        self.http = http or httpx.Client(timeout=30)
        self.quota_used = 0

    def _get(self, endpoint: str, params: dict) -> dict:
        resp = self.http.get(f"{API}/{endpoint}", params=params,
                             headers={"X-Goog-Api-Key": self.api_key})
        self.quota_used += COST[endpoint]
        if resp.status_code == 403 and "quota" in resp.text.lower():
            raise QuotaExceeded("YouTube API daily quota exceeded; try again tomorrow.")
        resp.raise_for_status()
        return resp.json()

    def search(self, query: str, lookback_days: int, max_results: int = 50,
               language: str = "", region: str = "") -> tuple[list[str], int]:
        after = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        params = {
            "part": "id", "q": query, "type": "video", "order": "viewCount",
            "maxResults": min(max_results, 50), "videoDuration": "short",  # < 4 min
            "publishedAfter": after.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if language:
            params["relevanceLanguage"] = language
        if region:
            params["regionCode"] = region
        data = self._get("search", params)
        ids = [i["id"]["videoId"] for i in data.get("items", []) if i.get("id", {}).get("videoId")]
        return ids, int(data.get("pageInfo", {}).get("totalResults", 0))

    def videos(self, ids: list[str]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(ids), 50):
            out += self._get("videos", {"part": "snippet,statistics,contentDetails",
                                        "id": ",".join(ids[i:i + 50]), "maxResults": 50}).get("items", [])
        return out

    def channels(self, ids: list[str] | None = None, handle: str | None = None) -> list[dict]:
        base = {"part": "snippet,statistics,contentDetails", "maxResults": 50}
        if handle:
            return self._get("channels", {**base, "forHandle": handle}).get("items", [])
        out: list[dict] = []
        for i in range(0, len(ids or []), 50):
            out += self._get("channels", {**base, "id": ",".join(ids[i:i + 50])}).get("items", [])
        return out

    def recent_upload_ids(self, uploads_playlist: str, limit: int = 30) -> list[str]:
        data = self._get("playlistItems", {"part": "contentDetails", "playlistId": uploads_playlist,
                                           "maxResults": min(limit, 50)})
        return [i["contentDetails"]["videoId"] for i in data.get("items", [])]


def _post(item: dict) -> Post:
    sn, st = item.get("snippet", {}), item.get("statistics", {})
    thumbs = sn.get("thumbnails", {})
    thumb = (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url")
    num = lambda k: int(st[k]) if k in st else None  # noqa: E731
    return Post(
        id=make_id("youtube", item["id"]), platform="youtube",
        creator_id=make_id("youtube", sn.get("channelId", "")),
        title=sn.get("title", ""), description=(sn.get("description") or "")[:2000],
        published_at=sn.get("publishedAt"),
        duration_s=scoring.parse_duration(item.get("contentDetails", {}).get("duration")),
        views=num("viewCount"), likes=num("likeCount"), comments=num("commentCount"),
        thumbnail=thumb, url=f"https://www.youtube.com/shorts/{item['id']}",
        language=sn.get("defaultAudioLanguage") or sn.get("defaultLanguage"),
    )


def _creator(item: dict) -> tuple[Creator, str | None]:
    st, sn = item.get("statistics", {}), item.get("snippet", {})
    subs = None if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount", 0))
    handle = sn.get("customUrl")
    return (Creator(id=make_id("youtube", item["id"]), platform="youtube", handle=handle,
                    title=sn.get("title"), followers=subs,
                    url=f"https://www.youtube.com/{handle}" if handle else
                    f"https://www.youtube.com/channel/{item['id']}"),
            item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads"))


class YouTubeProvider:
    platform = "youtube"
    name = "YouTube Data API (official)"
    can_search = True

    def __init__(self, api_key: str, client: YouTubeClient | None = None, language: str = "", region: str = ""):
        self.client = client or YouTubeClient(api_key)
        self.language, self.region = language, region

    @property
    def usage(self) -> int:
        return self.client.quota_used

    def search(self, query: str, lookback_days: int, limit: int) -> tuple[list[Post], int | None]:
        ids, total = self.client.search(query, lookback_days, limit, self.language, self.region)
        return ([_post(v) for v in self.client.videos(ids)] if ids else []), total

    def _recent(self, uploads: str, sample_size: int) -> list[Post]:
        ids = self.client.recent_upload_ids(uploads, sample_size)
        return [_post(v) for v in self.client.videos(ids)] if ids else []

    def profiles(self, creator_ids, sample_size, follower_range=None) -> list[Profile]:
        out = []
        for item in self.client.channels([split_id(c)[1] for c in creator_ids]):
            creator, uploads = _creator(item)
            in_range = follower_range is None or (
                creator.followers is not None and follower_range[0] <= creator.followers <= follower_range[1])
            recent = self._recent(uploads, sample_size) if (in_range and uploads) else None
            out.append(Profile(creator, recent))
        return out

    def lookup_creator(self, handle_or_url: str, sample_size: int) -> Profile:
        text = handle_or_url.strip()
        if m := re.search(r"/channel/(UC[\w-]+)", text):
            items = self.client.channels([m.group(1)])
        else:
            m = re.search(r"@([\w.\-]+)", text)
            handle = "@" + (m.group(1) if m else text.lstrip("@").strip("/"))
            items = self.client.channels(handle=handle)
        if not items:
            raise ProviderUnavailable(f"No YouTube channel found for '{handle_or_url}'.")
        creator, uploads = _creator(items[0])
        return Profile(creator, self._recent(uploads, sample_size) if uploads else [])

    def refresh_media_url(self, post: Post) -> str | None:
        return None  # YouTube has no official download; media.py can use yt-dlp if allowed
