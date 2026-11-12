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

    def handler(req: httpx.Request):
        calls.append((req.url.path, req.headers["authorization"], json.loads(req.content)))
        return httpx.Response(200, json=[TT_ITEM, {"error": "blocked"}])

    prov = TikTokScraperProvider("tok", ApifyClient("tok", mock_client(handler)))
    posts, supply = prov.search("fitfam", lookback_days=3650, limit=20)
    path, auth, body = calls[0]
    assert path == "/v2/acts/clockworks~tiktok-scraper/run-sync-get-dataset-items"
    assert auth == "Bearer tok" and body["hashtags"] == ["fitfam"] and body["resultsPerPage"] == 20
    assert len(posts) == 1 and supply is None and prov.usage == 1

    prov.search("abs workout", 3650, 20)
    assert calls[-1][2]["searchQueries"] == ["abs workout"]  # phrases use keyword search

    profile = prov.lookup_creator("https://www.tiktok.com/@GymGuy", 30)
    assert calls[-1][2]["profiles"] == ["gymguy"] and profile.creator.followers == 12_000
    assert len(profile.recent_posts) == 1


def test_apify_bad_token():
    prov = TikTokScraperProvider("bad", ApifyClient("bad", mock_client(lambda r: httpx.Response(401))))
    with pytest.raises(ProviderUnavailable):
        prov.search("x", 30, 5)


def test_instagram_scraper_details():
    def handler(req):
        body = json.loads(req.content)
        if body["resultsType"] == "details":
            return httpx.Response(200, json=[{"username": "MoneyMia", "fullName": "Mia", "followersCount": 8800,
                                              "latestPosts": [IG_ITEM, {**IG_ITEM, "type": "Image"}]}])
        assert body["directUrls"] == ["https://www.instagram.com/explore/tags/personalfinance/"]
        return httpx.Response(200, json=[IG_ITEM])

    prov = InstagramScraperProvider("tok", ApifyClient("tok", mock_client(handler)))
    posts, _ = prov.search("personal finance", 3650, 10)
    assert posts[0].views == 250_000
    prof = prov.lookup_creator("@MoneyMia", 30)
    assert prof.creator.followers == 8800 and len(prof.recent_posts) == 1


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
