"""A fake YouTube client with synthetic data.

Used for demo mode (no API key needed) and for tests. It mimics the real API's
response shapes and quota costs, so the real scanner code runs unchanged.
"""
from __future__ import annotations

import math
import random
import zlib
from datetime import datetime, timedelta, timezone

from .youtube import COST

TITLE_TEMPLATES = [
    "I tried {kw} for 30 days (honest results)",
    "Stop doing this if you care about {kw}",
    "3 {kw} mistakes beginners make",
    "Nobody tells you this about {kw}",
    "{kw} explained in 60 seconds",
    "The {kw} hack that changed everything",
    "Why your {kw} isn't working",
    "POV: you finally figured out {kw}",
    "Rating viral {kw} advice",
    "Do this before you start {kw}",
]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeYouTubeClient:
    def __init__(self, seed: int = 7, n_channels: int = 150):
        self.quota_used = 0
        self.now = datetime.now(timezone.utc)
        self._rng = random.Random(seed)
        self._videos: dict[str, dict] = {}
        self._channels: dict[str, dict] = {}
        self._uploads: dict[str, list[str]] = {}
        self._counter = 0
        for i in range(n_channels):
            self._make_channel(i)

    # --- synthetic data -------------------------------------------------
    def _make_channel(self, i: int) -> None:
        rng = self._rng
        cid = f"demo_ch_{i:03d}"
        subs = int(10 ** rng.uniform(2.3, 6.4))
        median = max(200, subs * 10 ** rng.uniform(-0.8, 0.7))
        self._channels[cid] = {
            "id": cid,
            "snippet": {"title": f"Demo Creator {i:03d}"},
            "statistics": {"subscriberCount": str(subs), "hiddenSubscriberCount": rng.random() < 0.03},
            "contentDetails": {"relatedPlaylists": {"uploads": f"UU{cid}"}},
            "_median": median,
        }
        self._uploads[cid] = [
            self._make_video(cid, "regular upload", median * math.exp(rng.gauss(0, 0.5)))
            for _ in range(30)
        ]

    def _make_video(self, cid: str, kw: str, views: float, title: str | None = None) -> str:
        rng = self._rng
        self._counter += 1
        vid = f"demo_v_{self._counter:05d}"
        published = self.now - timedelta(days=rng.uniform(3, 88), hours=rng.uniform(0, 23))
        views = int(views)
        self._videos[vid] = {
            "id": vid,
            "snippet": {
                "channelId": cid,
                "title": title or rng.choice(TITLE_TEMPLATES).format(kw=kw),
                "description": f"Synthetic demo video about {kw}.",
                "publishedAt": _iso(published),
                "thumbnails": {},
            },
            "statistics": {
                "viewCount": str(views),
                "likeCount": str(int(views * rng.uniform(0.02, 0.08))),
                "commentCount": str(int(views * rng.uniform(0.001, 0.005))),
            },
            "contentDetails": {"duration": f"PT{rng.randint(12, 59)}S"},
        }
        return vid

    # --- API surface (same as YouTubeClient) ----------------------------
    def search(self, query: str, lookback_days: int, max_results: int = 50,
               shorts_only: bool = True) -> tuple[list[str], int]:
        self.quota_used += COST["search"]
        rng = random.Random(zlib.crc32(query.encode()))
        ids = []
        for _ in range(min(max_results, 50)):
            cid = f"demo_ch_{rng.randrange(len(self._channels)):03d}"
            median = self._channels[cid]["_median"]
            # Heavy-tailed multiplier: most videos are normal, a few explode.
            mult = math.exp(rng.gauss(0.6, 1.3))
            title = TITLE_TEMPLATES[rng.randrange(len(TITLE_TEMPLATES))].format(kw=query)
            vid = self._make_video(cid, query, median * mult, title)
            self._uploads[cid].insert(0, vid)
            ids.append(vid)
        supply = int(10 ** rng.uniform(3.3, 5.8))
        return ids, supply

    def videos(self, ids: list[str]) -> list[dict]:
        self.quota_used += COST["videos"] * max(1, math.ceil(len(ids) / 50))
        return [self._videos[i] for i in ids if i in self._videos]

    def channels(self, ids: list[str]) -> list[dict]:
        self.quota_used += COST["channels"] * max(1, math.ceil(len(ids) / 50))
        return [{k: v for k, v in self._channels[i].items() if not k.startswith("_")}
                for i in ids if i in self._channels]

    def recent_upload_ids(self, uploads_playlist: str, limit: int = 30) -> list[str]:
        self.quota_used += COST["playlistItems"]
        return self._uploads.get(uploads_playlist.removeprefix("UU"), [])[:limit]
