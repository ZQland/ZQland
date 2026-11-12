"""TikTok and Instagram via Apify scraper actors (third-party, paid per result).

These collect public data the platforms don't offer through official APIs. That goes
against TikTok's and Instagram's terms of service: fine for personal research, a real
risk in a product you sell. Switch a platform to "official" or "off" in niches.yaml.

Actor field names below follow each actor's published sample output. Actors change
over time: if a scan returns posts with missing numbers, run the actor once in the
Apify console, compare its output with the getters below, and adjust them.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

import httpx

from ..models import Creator, Post, Profile, make_id, split_id
from .base import ProviderUnavailable

APIFY = "https://api.apify.com/v2"
TIKTOK_ACTOR = "clockworks~tiktok-scraper"
INSTAGRAM_ACTOR = "apify~instagram-scraper"


def dig(item: dict, path: str, default=None):
    """Read 'a.b.c' from nested dicts, or from a flattened 'a.b.c' key."""
    if path in item:
        return item[path]
    cur = item
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _iso_from_unix(ts) -> str | None:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None


def _within(post: Post, lookback_days: int) -> bool:
    if not post.published_at:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    return datetime.fromisoformat(post.published_at.replace("Z", "+00:00")) >= cutoff


class ApifyClient:
    """Starts an actor run, polls until it finishes, then reads its dataset.

    (Apify's one-call "run-sync" endpoint holds a connection open for the whole run,
    which proxies and gateways tend to cut off with a 502, so this polls instead.)
    """

    POLL_SECONDS = 20          # each status request waits up to this long on Apify's side
    MAX_RUN_SECONDS = 15 * 60  # give up on a run after this long
    _DONE = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}

    def __init__(self, token: str, http: httpx.Client | None = None):
        self.token = token
        self.http = http or httpx.Client(timeout=60)
        self.results = 0       # Apify bills these actors per result
        self.cost_usd = 0.0    # as reported by Apify for finished runs

    def _call(self, method: str, path: str, **kw) -> httpx.Response:
        resp = self.http.request(method, f"{APIFY}{path}",
                                 headers={"Authorization": f"Bearer {self.token}"}, **kw)
        if resp.status_code in (401, 403):
            raise ProviderUnavailable("Apify rejected the token. Check APIFY_TOKEN.")
        resp.raise_for_status()
        return resp

    def run(self, actor: str, actor_input: dict) -> list[dict]:
        run = self._call("POST", f"/acts/{actor}/runs", params={"waitForFinish": self.POLL_SECONDS},
                         json=actor_input).json()["data"]
        deadline = time.monotonic() + self.MAX_RUN_SECONDS
        while run["status"] not in self._DONE:
            if time.monotonic() > deadline:
                raise ProviderUnavailable(f"Apify run {run['id']} took longer than 15 minutes.")
            run = self._call("GET", f"/actor-runs/{run['id']}",
                             params={"waitForFinish": self.POLL_SECONDS}).json()["data"]
        self.cost_usd += float(run.get("usageTotalUsd") or 0)
        if run["status"] != "SUCCEEDED":
            raise ProviderUnavailable(f"Apify run {run['id']} ended with status {run['status']}.")
        items = self._call("GET", f"/datasets/{run['defaultDatasetId']}/items",
                           params={"clean": "true", "format": "json"}).json()
        items = [i for i in items if isinstance(i, dict) and not i.get("error")]
        self.results += len(items)
        return items


# --- TikTok -----------------------------------------------------------------

def tiktok_post(item: dict) -> Post | None:
    native = str(item.get("id") or "")
    username = dig(item, "authorMeta.name")
    if not native or not username or item.get("isAd") or item.get("isSponsored"):
        return None  # skip paid posts: their views don't reflect organic reach
    return Post(
        id=make_id("tiktok", native), platform="tiktok",
        creator_id=make_id("tiktok", username.lower()),
        title=(item.get("text") or "")[:300], description=item.get("text") or "",
        published_at=item.get("createTimeISO") or _iso_from_unix(item.get("createTime")),
        duration_s=_int(dig(item, "videoMeta.duration")) or 0,
        views=_int(item.get("playCount")), likes=_int(item.get("diggCount")),
        comments=_int(item.get("commentCount")), shares=_int(item.get("shareCount")),
        saves=_int(item.get("collectCount")),
        thumbnail=dig(item, "videoMeta.coverUrl") or dig(item, "videoMeta.originalCoverUrl"),
        url=item.get("webVideoUrl") or f"https://www.tiktok.com/@{username}/video/{native}",
        media_url=(item.get("mediaUrls") or [None])[0],
        creator_followers=_int(dig(item, "authorMeta.fans")),
        language=item.get("textLanguage") or None,
    )


def tiktok_creator(item: dict) -> Creator:
    username = dig(item, "authorMeta.name")
    return Creator(id=make_id("tiktok", username.lower()), platform="tiktok", handle=username,
                   title=dig(item, "authorMeta.nickName") or username,
                   followers=_int(dig(item, "authorMeta.fans")),
                   url=f"https://www.tiktok.com/@{username}")


def _tiktok_username(text: str) -> str:
    m = re.search(r"tiktok\.com/@([\w.\-]+)", text)
    return (m.group(1) if m else text.strip().lstrip("@").strip("/")).lower()


class TikTokScraperProvider:
    platform = "tiktok"
    name = "Apify TikTok scraper (third-party)"
    can_search = True

    def __init__(self, token: str, client: ApifyClient | None = None):
        self.client = client or ApifyClient(token)

    @property
    def usage(self) -> int:
        return self.client.results

    def _base(self, limit: int) -> dict:
        return {"resultsPerPage": limit, "shouldDownloadVideos": False,
                "shouldDownloadCovers": False, "shouldDownloadSubtitles": False}

    def search(self, query: str, lookback_days: int, limit: int):
        # Single words behave like hashtags on TikTok (#fitfam); phrases use keyword search.
        key = {"hashtags": [query]} if " " not in query.strip() else {"searchQueries": [query]}
        items = self.client.run(TIKTOK_ACTOR, {**self._base(limit), **key})
        posts = [p for p in map(tiktok_post, items) if p and _within(p, lookback_days)]
        return posts, None  # TikTok gives no reliable count of matching uploads

    def _profiles_by_username(self, usernames: list[str], sample_size: int) -> list[Profile]:
        items = self.client.run(TIKTOK_ACTOR, {**self._base(sample_size), "profiles": usernames,
                                               "profileSorting": "latest"})
        grouped: dict[str, Profile] = {}
        for item in items:
            post = tiktok_post(item)
            if not post:
                continue
            prof = grouped.setdefault(post.creator_id, Profile(tiktok_creator(item), []))
            prof.recent_posts.append(post)
        return list(grouped.values())

    def profiles(self, creator_ids, sample_size, follower_range=None):
        # One actor run returns follower counts and recent posts together.
        return self._profiles_by_username([split_id(c)[1] for c in creator_ids], sample_size)

    def lookup_creator(self, handle_or_url: str, sample_size: int) -> Profile:
        found = self._profiles_by_username([_tiktok_username(handle_or_url)], sample_size)
        if not found:
            raise ProviderUnavailable(f"No TikTok account found for '{handle_or_url}'.")
        return found[0]

    def refresh_media_url(self, post: Post) -> str | None:
        items = self.client.run(TIKTOK_ACTOR, {**self._base(1), "postURLs": [post.url],
                                               "shouldDownloadVideos": True})
        for item in items:
            urls = item.get("mediaUrls") or []
            if urls:
                return urls[0]
        return None


# --- Instagram --------------------------------------------------------------

def instagram_post(item: dict, owner_fallback: str | None = None) -> Post | None:
    is_video = item.get("type") == "Video" or item.get("productType") == "clips" or item.get("isVideo")
    username = item.get("ownerUsername") or owner_fallback
    code = item.get("shortCode") or item.get("shortcode")
    if not is_video or not username or not (code or item.get("id")):
        return None
    views = _int(item.get("videoPlayCount")) or _int(item.get("videoViewCount"))
    likes = _int(item.get("likesCount"))
    return Post(
        id=make_id("instagram", code or str(item["id"])), platform="instagram",
        creator_id=make_id("instagram", username.lower()),
        title=(item.get("caption") or "")[:300], description=item.get("caption") or "",
        published_at=item.get("timestamp"),
        duration_s=int(float(item.get("videoDuration") or 0)),
        views=views, likes=likes if likes and likes > 0 else None,  # -1/0 when likes are hidden
        comments=_int(item.get("commentsCount")),
        thumbnail=item.get("displayUrl"),
        url=item.get("url") or (f"https://www.instagram.com/reel/{code}/" if code else None),
        media_url=item.get("videoUrl"),
    )


def _ig_username(text: str) -> str:
    m = re.search(r"instagram\.com/([\w.]+)", text)
    return (m.group(1) if m else text.strip().lstrip("@").strip("/")).lower()


class InstagramScraperProvider:
    platform = "instagram"
    name = "Apify Instagram scraper (third-party)"
    can_search = True

    def __init__(self, token: str, client: ApifyClient | None = None):
        self.client = client or ApifyClient(token)

    @property
    def usage(self) -> int:
        return self.client.results

    def search(self, query: str, lookback_days: int, limit: int):
        # "reels" mode: the default "posts" mode returns a hashtag's top posts, which are mostly photos.
        tag = re.sub(r"[^\w]", "", query.lower())  # Instagram hashtags are one word
        items = self.client.run(INSTAGRAM_ACTOR, {
            "directUrls": [f"https://www.instagram.com/explore/tags/{tag}/"],
            "resultsType": "reels", "resultsLimit": limit})
        posts = [p for p in map(instagram_post, items) if p and _within(p, lookback_days)]
        return posts, None

    def _details(self, usernames: list[str], sample_size: int) -> list[Profile]:
        """Follower counts come from "details"; play counts for recent Reels only come from
        "reels" mode ("details" lists recent posts but without view or play counts)."""
        urls = [f"https://www.instagram.com/{u}/" for u in usernames]
        details = self.client.run(INSTAGRAM_ACTOR, {"directUrls": urls, "resultsType": "details",
                                                    "resultsLimit": 1})
        reels = self.client.run(INSTAGRAM_ACTOR, {"directUrls": urls, "resultsType": "reels",
                                                  "resultsLimit": sample_size})
        by_owner: dict[str, list[Post]] = {}
        for item in reels:
            p = instagram_post(item)
            if p:
                by_owner.setdefault(p.creator_id, []).append(p)
        out = []
        for item in details:
            username = item.get("username")
            if not username:
                continue
            creator = Creator(id=make_id("instagram", username.lower()), platform="instagram",
                              handle=username, title=item.get("fullName") or username,
                              followers=_int(item.get("followersCount")),
                              url=f"https://www.instagram.com/{username}/")
            out.append(Profile(creator, by_owner.get(creator.id, [])))
        return out

    def profiles(self, creator_ids, sample_size, follower_range=None):
        return self._details([split_id(c)[1] for c in creator_ids], sample_size)

    def lookup_creator(self, handle_or_url: str, sample_size: int) -> Profile:
        found = self._details([_ig_username(handle_or_url)], sample_size)
        if not found:
            raise ProviderUnavailable(f"No Instagram account found for '{handle_or_url}'.")
        return found[0]

    def refresh_media_url(self, post: Post) -> str | None:
        items = self.client.run(INSTAGRAM_ACTOR, {"directUrls": [post.url], "resultsType": "posts",
                                                  "resultsLimit": 1})
        return next((i.get("videoUrl") for i in items if i.get("videoUrl")), None)
