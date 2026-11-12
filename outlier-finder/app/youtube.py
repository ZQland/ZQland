"""Thin YouTube Data API v3 client with quota tracking."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

API = "https://www.googleapis.com/youtube/v3"

# Official per-call quota costs.
COST = {"search": 100, "videos": 1, "channels": 1, "playlistItems": 1}


class QuotaExceeded(RuntimeError):
    pass


class YouTubeClient:
    def __init__(self, api_key: str, http: httpx.Client | None = None):
        self.api_key = api_key
        self.http = http or httpx.Client(timeout=30)
        self.quota_used = 0

    def _get(self, endpoint: str, params: dict) -> dict:
        resp = self.http.get(
            f"{API}/{endpoint}", params=params, headers={"X-Goog-Api-Key": self.api_key}
        )
        self.quota_used += COST[endpoint]
        if resp.status_code == 403 and "quota" in resp.text.lower():
            raise QuotaExceeded("YouTube API daily quota exceeded; try again tomorrow.")
        resp.raise_for_status()
        return resp.json()

    def search(self, query: str, lookback_days: int, max_results: int = 50,
               shorts_only: bool = True) -> tuple[list[str], int]:
        """Top videos for a query by view count. Returns (video_ids, approx_total)."""
        after = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        params = {
            "part": "id",
            "q": query,
            "type": "video",
            "order": "viewCount",
            "maxResults": min(max_results, 50),
            "publishedAfter": after.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if shorts_only:
            params["videoDuration"] = "short"  # under 4 minutes; refined later
        data = self._get("search", params)
        ids = [item["id"]["videoId"] for item in data.get("items", [])
               if item.get("id", {}).get("videoId")]
        total = int(data.get("pageInfo", {}).get("totalResults", 0))
        return ids, total

    def videos(self, ids: list[str]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(ids), 50):
            data = self._get("videos", {
                "part": "snippet,statistics,contentDetails",
                "id": ",".join(ids[i:i + 50]),
                "maxResults": 50,
            })
            out.extend(data.get("items", []))
        return out

    def channels(self, ids: list[str]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(ids), 50):
            data = self._get("channels", {
                "part": "snippet,statistics,contentDetails",
                "id": ",".join(ids[i:i + 50]),
                "maxResults": 50,
            })
            out.extend(data.get("items", []))
        return out

    def recent_upload_ids(self, uploads_playlist: str, limit: int = 30) -> list[str]:
        data = self._get("playlistItems", {
            "part": "contentDetails",
            "playlistId": uploads_playlist,
            "maxResults": min(limit, 50),
        })
        return [i["contentDetails"]["videoId"] for i in data.get("items", [])]
