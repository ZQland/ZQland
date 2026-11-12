"""Real provider adapters against mocked HTTP: request shape in, normalized Posts out."""
import json

import httpx
import pytest

from app.providers.apify import (ApifyClient, InstagramScraperProvider, TikTokScraperProvider, dig,
                                 instagram_post, tiktok_post)
from app.providers.base import ProviderUnavailable
from app.providers.instagram_official import InstagramOfficialProvider
from app.providers.youtube import YouTubeClient, YouTubeProvider

TT_ITEM = {
    "id": "7300000000000000001", "text": "3 abs mistakes #fitness", "createTimeISO": "2026-11-01T10:00:00.000Z",
    "playCount": 1_200_000, "diggCount": 90_000, "commentCount": 1_500, "shareCount": 4_000, "collectCount": 12_000,
    "webVideoUrl": "https://www.tiktok.com/@gymguy/video/7300000000000000001",
    "authorMeta": {"name": "GymGuy", "nickName": "Gym Guy", "fans": 12_000},
    "videoMeta": {"duration": 34, "coverUrl": "https://p16.example/cover.jpg"},
}
TT_FLAT = {"id": "7300000000000000002", "text": "flat", "createTime": 1762000000, "playCount": 5,
           "authorMeta.name": "flatuser", "authorMeta.fans": 77, "videoMeta.duration": 12}
IG_ITEM = {"id": "1", "shortCode": "Cabc123", "type": "Video", "caption": "Budget like this", "timestamp":
           "2026-11-02T08:00:00.000Z", "likesCount": 4000, "commentsCount": 210, "videoPlayCount": 250_000,
           "videoViewCount": 90_000, "videoDuration": 27.5, "ownerUsername": "MoneyMia",
           "url": "https://www.instagram.com/p/Cabc123/", "videoUrl": "https://cdn.example/v.mp4",
           "displayUrl": "https://cdn.example/t.jpg"}


def mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def apify_mock(items_for, calls=None, statuses=("RUNNING", "SUCCEEDED")):
    """Mimics Apify: POST /acts/{actor}/runs → poll /actor-runs/{id} → GET /datasets/{id}/items."""
    state = {"body": None, "polls": list(statuses[1:])}

    def handler(req: httpx.Request):
        path = req.url.path
        if req.method == "POST" and path.endswith("/runs"):
            state["body"] = json.loads(req.content)
            if calls is not None:
                calls.append((path, req.headers.get("authorization"), state["body"]))
            return httpx.Response(201, json={"data": {"id": "run1", "status": statuses[0],
                                                       "defaultDatasetId": "ds1", "usageTotalUsd": 0.01}})
        if path.startswith("/v2/actor-runs/"):
            status = state["polls"].pop(0) if state["polls"] else "SUCCEEDED"
            return httpx.Response(200, json={"data": {"id": "run1", "status": status,
                                                       "defaultDatasetId": "ds1", "usageTotalUsd": 0.01}})
        if path == "/v2/datasets/ds1/items":
            return httpx.Response(200, json=items_for(state["body"]))
        return httpx.Response(404)
    return handler


def test_dig_reads_nested_and_flat_keys():
    assert dig(TT_ITEM, "authorMeta.fans") == 12_000
    assert dig(TT_FLAT, "authorMeta.fans") == 77
    assert dig(TT_ITEM, "nope.x", 3) == 3


def test_tiktok_post_normalizes_fields():
    p = tiktok_post(TT_ITEM)
    assert p.id == "tiktok:7300000000000000001" and p.creator_id == "tiktok:gymguy"
    assert (p.views, p.likes, p.comments, p.shares, p.saves) == (1_200_000, 90_000, 1_500, 4_000, 12_000)
    assert p.duration_s == 34 and p.creator_followers == 12_000 and p.published_at.startswith("2026-11-01")
    flat = tiktok_post(TT_FLAT)
    assert flat.creator_id == "tiktok:flatuser" and flat.published_at.endswith("Z") and flat.duration_s == 12


def test_instagram_post_prefers_plays_and_skips_photos():
    p = instagram_post(IG_ITEM)
    assert p.id == "instagram:Cabc123" and p.creator_id == "instagram:moneymia"
    assert p.views == 250_000 and p.duration_s == 27 and p.media_url.endswith("v.mp4")
    assert instagram_post({**IG_ITEM, "type": "Image", "productType": None}) is None
    assert instagram_post({**IG_ITEM, "likesCount": -1}).likes is None  # hidden likes


def test_tiktok_scraper_requests():
    calls = []
    client = ApifyClient("tok", mock_client(apify_mock(lambda body: [TT_ITEM, {"error": "blocked"},
                                                                    {**TT_ITEM, "id": "9", "isAd": True}], calls)))
    client.POLL_SECONDS = 0
    prov = TikTokScraperProvider("tok", client)
    posts, supply = prov.search("fitfam", lookback_days=3650, limit=20)
    path, auth, body = calls[0]
    assert path == "/v2/acts/clockworks~tiktok-scraper/runs"
    assert auth == "Bearer tok" and body["hashtags"] == ["fitfam"] and body["resultsPerPage"] == 20
    assert [p.id for p in posts] == ["tiktok:7300000000000000001"]  # error rows and ads dropped
    assert supply is None and client.cost_usd == 0.01

    prov.search("abs workout", 3650, 20)
    assert calls[-1][2]["searchQueries"] == ["abs workout"]  # phrases use keyword search

    profile = prov.lookup_creator("https://www.tiktok.com/@GymGuy", 30)
    assert calls[-1][2]["profiles"] == ["gymguy"] and profile.creator.followers == 12_000
    assert len(profile.recent_posts) == 1


def test_apify_failed_run_is_reported():
    client = ApifyClient("tok", mock_client(apify_mock(lambda b: [], statuses=("RUNNING", "FAILED"))))
    with pytest.raises(ProviderUnavailable, match="FAILED"):
        client.run("a~b", {})


def test_apify_bad_token():
    prov = TikTokScraperProvider("bad", ApifyClient("bad", mock_client(lambda r: httpx.Response(401))))
    with pytest.raises(ProviderUnavailable):
        prov.search("x", 30, 5)


def test_instagram_scraper_details():
    seen = []

    def items_for(body):
        seen.append(body)
        if body["resultsType"] == "details":  # real "details" output has no view counts
            return [{"username": "MoneyMia", "fullName": "Mia", "followersCount": 8800,
                     "latestPosts": [{**IG_ITEM, "videoPlayCount": None, "videoViewCount": None}]}]
        if body["directUrls"] == ["https://www.instagram.com/moneymia/"]:
            return [IG_ITEM, {**IG_ITEM, "type": "Image", "productType": "feed"}]
        assert body["directUrls"] == ["https://www.instagram.com/explore/tags/personalfinance/"]
        return [IG_ITEM]

    prov = InstagramScraperProvider("tok", ApifyClient("tok", mock_client(apify_mock(items_for, statuses=("SUCCEEDED",)))))
    posts, _ = prov.search("personal finance", 3650, 10)
    assert posts[0].views == 250_000 and seen[0]["resultsType"] == "reels"
    prof = prov.lookup_creator("@MoneyMia", 30)
    assert prof.creator.followers == 8800
    assert [p.views for p in prof.recent_posts] == [250_000]  # plays come from "reels" mode
    assert seen[-1] == {"directUrls": ["https://www.instagram.com/moneymia/"], "resultsType": "reels",
                        "resultsLimit": 30}


def test_instagram_official_business_discovery():
    seen = {}

    def handler(req):
        seen["url"] = req.url.params["fields"]
        return httpx.Response(200, json={"business_discovery": {
            "username": "moneymia", "name": "Mia", "followers_count": 8800, "media": {"data": [
                {"id": "17", "media_type": "VIDEO", "like_count": 300, "comments_count": 12, "view_count": 40000,
                 "timestamp": "2026-11-01T10:00:00+0000", "permalink": "https://www.instagram.com/reel/Cxyz/",
                 "caption": "Save this"},
                {"id": "18", "media_type": "IMAGE", "like_count": 5}]}}})

    prov = InstagramOfficialProvider("token", "1784", http=mock_client(handler))
    prof = prov.lookup_creator("https://www.instagram.com/moneymia/", 25)
    assert "business_discovery.username(moneymia)" in seen["url"] and "media.limit(25)" in seen["url"]
    assert prof.creator.followers == 8800 and [p.id for p in prof.recent_posts] == ["instagram:Cxyz"]
    assert prof.recent_posts[0].published_at == "2026-11-01T10:00:00Z" and prof.recent_posts[0].views == 40000
    with pytest.raises(ProviderUnavailable):
        prov.search("x", 30, 5)


def test_youtube_provider_lookup_by_handle_and_quota():
    def handler(req):
        p = req.url.path.rsplit("/", 1)[-1]
        assert req.headers["x-goog-api-key"] == "k"
        if p == "channels":
            assert req.url.params["forHandle"] == "@coach"
            return httpx.Response(200, json={"items": [{"id": "UC1", "snippet": {"title": "Coach", "customUrl": "@coach"},
                                                        "statistics": {"subscriberCount": "5400"},
                                                        "contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]})
        if p == "playlistItems":
            return httpx.Response(200, json={"items": [{"contentDetails": {"videoId": "v1"}}]})
        return httpx.Response(200, json={"items": [{"id": "v1", "snippet": {"channelId": "UC1", "title": "t",
                                                                           "publishedAt": "2026-10-01T00:00:00Z"},
                                                    "statistics": {"viewCount": "999"},
                                                    "contentDetails": {"duration": "PT45S"}}]})

    prov = YouTubeProvider("k", YouTubeClient("k", mock_client(handler)))
    prof = prov.lookup_creator("https://www.youtube.com/@coach", 30)
    assert prof.creator.id == "youtube:UC1" and prof.creator.followers == 5400
    assert prof.recent_posts[0].id == "youtube:v1" and prof.recent_posts[0].duration_s == 45
    assert prov.usage == 3
