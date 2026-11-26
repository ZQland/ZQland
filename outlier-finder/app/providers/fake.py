"""Synthetic data for demo mode and tests. Mimics a real provider for any platform."""
from __future__ import annotations

import math
import random
import zlib
from datetime import datetime, timedelta, timezone

from ..models import Creator, Post, Profile, make_id, split_id
from .base import ProviderUnavailable

TITLES = [
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


class FakeProvider:
    can_search = True

    def __init__(self, platform: str = "youtube", seed: int = 7, n_creators: int = 150):
        self.platform = platform
        self.name = f"Demo data ({platform})"
        self.usage = 0
        self.now = datetime.now(timezone.utc)
        self._rng = random.Random(f"{seed}-{platform}")
        self._creators: dict[str, dict] = {}
        self._posts: dict[str, Post] = {}
        self._uploads: dict[str, list[str]] = {}
        self._counter = 0
        for i in range(n_creators):
            self._make_creator(i)

    # --- synthetic data -------------------------------------------------
    def _make_creator(self, i: int) -> None:
        rng = self._rng
        cid = make_id(self.platform, f"demo_creator_{i:03d}")
        followers = int(10 ** rng.uniform(2.3, 6.4))
        median = max(200, followers * 10 ** rng.uniform(-0.8, 0.7))
        self._creators[cid] = {"followers": followers, "median": median, "i": i,
                               "hidden": rng.random() < 0.03}
        self._uploads[cid] = []
        for _ in range(30):
            self._uploads[cid].append(self._make_post(cid, "regular upload", median * math.exp(rng.gauss(0, 0.5))))

    def _make_post(self, cid: str, kw: str, views: float, title: str | None = None,
                   rng: random.Random | None = None) -> str:
        rng = rng or self._rng
        self._counter += 1
        native = f"demo_v_{self._counter:05d}"
        views = int(views)
        post = Post(
            id=make_id(self.platform, native), platform=self.platform, creator_id=cid,
            title=title or rng.choice(TITLES).format(kw=kw),
            description=f"Synthetic demo video about {kw}.",
            published_at=_iso(self.now - timedelta(days=rng.uniform(3, 88), hours=rng.uniform(0, 23))),
            duration_s=rng.randint(12, 59), views=views,
            likes=int(views * rng.uniform(0.02, 0.09)), comments=int(views * rng.uniform(0.001, 0.005)),
            shares=None if self.platform == "youtube" else int(views * rng.uniform(0.001, 0.02)),
            saves=None if self.platform == "youtube" else int(views * rng.uniform(0.002, 0.03)),
            media_url=f"demo://{native}",
        )
        self._posts[post.id] = post
        return post.id

    def _creator(self, cid: str) -> Creator:
        c = self._creators[cid]
        name = f"Demo {self.platform.title()} Creator {c['i']:03d}"
        return Creator(id=cid, platform=self.platform, handle=f"demo_creator_{c['i']:03d}",
                       title=name, followers=None if c["hidden"] else c["followers"])

    def follower_history(self, creator_id: str, days: int = 90) -> list[tuple[str, int]]:
        """Daily follower counts, with jumps after each post scaled by its views."""
        c = self._creators.get(creator_id)
        if not c or c["hidden"]:
            return []
        daily = {d: c["followers"] * 0.0008 for d in range(days + 1)}  # organic growth
        for pid in self._uploads[creator_id]:
            p = self._posts[pid]
            age = (self.now - datetime.fromisoformat(p.published_at.replace("Z", "+00:00"))).days
            boost = max(p.views - c["median"], 0) * 0.004
            for k, share in ((0, 0.5), (1, 0.3), (2, 0.2)):  # gains land over ~3 days
                if 0 <= age - k <= days:
                    daily[age - k] += boost * share
        out, followers = [], float(c["followers"])
        for d in range(days + 1):  # walk backwards from today
            out.append((_iso(self.now - timedelta(days=d)), max(int(followers), 10)))
            followers -= daily[d]
        return list(reversed(out))

    # --- provider API ---------------------------------------------------
    def search(self, query: str, lookback_days: int, limit: int):
        self.usage += 100 if self.platform == "youtube" else limit
        rng = random.Random(zlib.crc32(f"{self.platform}:{query}".encode()))
        ids = []
        for _ in range(min(limit, 50)):
            cid = make_id(self.platform, f"demo_creator_{rng.randrange(len(self._creators)):03d}")
            mult = math.exp(rng.gauss(0.6, 1.3))  # heavy tail: a few posts explode
            title = TITLES[rng.randrange(len(TITLES))].format(kw=query)
            pid = self._make_post(cid, query, self._creators[cid]["median"] * mult, title, rng)
            self._uploads[cid].insert(0, pid)
            ids.append(pid)
        supply = int(10 ** rng.uniform(3.3, 5.8)) if self.platform == "youtube" else None
        return [self._posts[i] for i in ids], supply

    def profiles(self, creator_ids, sample_size, follower_range=None):
        self.usage += len(creator_ids) * (2 if self.platform == "youtube" else 10)
        out = []
        for cid in creator_ids:
            if cid not in self._creators:
                continue
            creator = self._creator(cid)
            in_range = follower_range is None or (
                creator.followers is not None and follower_range[0] <= creator.followers <= follower_range[1])
            recent = [self._posts[p] for p in self._uploads[cid][:sample_size]] if in_range else None
            out.append(Profile(creator, recent))
        return out

    def lookup_creator(self, handle_or_url: str, sample_size: int) -> Profile:
        text = handle_or_url.strip().lstrip("@").rstrip("/").split("/")[-1].lstrip("@")
        cid = make_id(self.platform, text if text.startswith("demo_creator_") else
                      f"demo_creator_{zlib.crc32(text.encode()) % len(self._creators):03d}")
        if cid not in self._creators:
            raise ProviderUnavailable(f"No demo creator for '{handle_or_url}'.")
        return self.profiles([cid], sample_size)[0]

    def refresh_media_url(self, post: Post) -> str | None:
        return f"demo://{split_id(post.id)[1]}"
