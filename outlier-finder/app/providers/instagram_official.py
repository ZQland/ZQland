"""Instagram Graph API "Business Discovery": the official, rules-compliant option.

Needs your own Instagram business/creator account connected to a Facebook app:
IG_ACCESS_TOKEN (long-lived token with instagram_basic) and IG_USER_ID (your
Instagram business account ID). It can only look up *business or creator* accounts
by username, so there's no keyword search: add creators to your watchlist instead.
Reel view counts come from `view_count` (added by Meta in 2025). If Meta doesn't
return it for an account, that creator's posts show no outlier score.
"""
from __future__ import annotations

import os
import re

import httpx

from ..models import Creator, Post, Profile, make_id, split_id
from .base import ProviderUnavailable

GRAPH = "https://graph.facebook.com"
MEDIA_FIELDS = ("id,caption,media_type,media_product_type,like_count,comments_count,"
                "view_count,timestamp,permalink,thumbnail_url,media_url")


class InstagramOfficialProvider:
    platform = "instagram"
    name = "Instagram Graph API, Business Discovery (official)"
    can_search = False

    def __init__(self, access_token: str, ig_user_id: str, http: httpx.Client | None = None):
        self.token, self.user_id = access_token, ig_user_id
        self.version = os.environ.get("IG_GRAPH_VERSION", "v24.0")
        self.http = http or httpx.Client(timeout=30)
        self.usage = 0  # API calls (rate limited per hour by Meta)

    def search(self, query, lookback_days, limit):
        raise ProviderUnavailable("Instagram's official API has no keyword search. "
                                  "Add creators on the Creators page instead.")

    def _discover(self, username: str, sample_size: int) -> Profile:
        fields = (f"business_discovery.username({username}){{id,username,name,followers_count,"
                  f"media_count,media.limit({sample_size}){{{MEDIA_FIELDS}}}}}")
        resp = self.http.get(f"{GRAPH}/{self.version}/{self.user_id}",
                             params={"fields": fields, "access_token": self.token})
        self.usage += 1
        if resp.status_code >= 400:
            msg = resp.json().get("error", {}).get("message", resp.text) if resp.content else resp.text
            raise ProviderUnavailable(f"Instagram API error for @{username}: {msg}")
        bd = resp.json().get("business_discovery", {})
        creator = Creator(id=make_id("instagram", username.lower()), platform="instagram",
                          handle=bd.get("username", username), title=bd.get("name"),
                          followers=bd.get("followers_count"),
                          url=f"https://www.instagram.com/{username}/")
        posts = []
        for m in bd.get("media", {}).get("data", []):
            if m.get("media_type") != "VIDEO":
                continue
            code = re.search(r"/(?:reel|p)/([\w-]+)", m.get("permalink") or "")
            native = code.group(1) if code else m["id"]
            posts.append(Post(
                id=make_id("instagram", native), platform="instagram", creator_id=creator.id,
                title=(m.get("caption") or "")[:300], description=m.get("caption") or "",
                published_at=(m.get("timestamp") or "").replace("+0000", "Z") or None,
                duration_s=0,  # not provided by Business Discovery
                views=m.get("view_count"), likes=m.get("like_count"),
                comments=m.get("comments_count"), thumbnail=m.get("thumbnail_url"),
                url=m.get("permalink"), media_url=m.get("media_url")))
        return Profile(creator, posts)

    def profiles(self, creator_ids, sample_size, follower_range=None):
        out = []
        for cid in creator_ids:
            try:
                out.append(self._discover(split_id(cid)[1], sample_size))
            except ProviderUnavailable:
                continue  # personal accounts can't be looked up; skip them
        return out

    def lookup_creator(self, handle_or_url: str, sample_size: int) -> Profile:
        m = re.search(r"instagram\.com/([\w.]+)", handle_or_url)
        return self._discover((m.group(1) if m else handle_or_url.strip().lstrip("@").strip("/")).lower(),
                              sample_size)

    def refresh_media_url(self, post: Post) -> str | None:
        return post.media_url  # only present when Meta allows it for that account
