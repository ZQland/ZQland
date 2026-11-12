"""Analysis pipeline (real ffmpeg on a synthetic clip) and the Claude request it sends."""
import json

import anthropic
import httpx2 as httpx  # the Anthropic SDK 2.x uses httpx2
import pytest
from fastapi.testclient import TestClient

from app import media, pipeline, queries
from app.analyzer import SCHEMA, SCORE_CRITERIA, SYSTEM_PROMPT, AnalysisError, ClaudeAnalyzer, FakeAnalyzer
from app.db import connect
from app.providers.fake import FakeProvider
from app.scanner import run_scan, start_scan
from app.transcribe import FakeTranscriber


@pytest.fixture()
def scanned(cfg):
    run_scan(cfg, start_scan(cfg, "ai"), ["ai"], ["tiktok"], {"tiktok": FakeProvider("tiktok")})
    return cfg


def test_demo_analysis_end_to_end(scanned, monkeypatch):
    cfg = scanned
    vid = queries.list_outliers(cfg)[0]["id"]
    pipeline.queue(cfg, vid)
    pipeline.run_analysis(cfg, vid)
    a = pipeline.get_analysis(cfg, vid)
    assert a["status"] == "done", a["error"]
    assert a["demo"] == 1 and a["breakdown"]["overall_score"]
    assert a["metrics"]["edit"]["cuts"] >= 4 and a["metrics"]["script"]["words"] > 30
    assert a["transcript"]["segments"][0]["words"]
    assert 4 <= len(a["frames"]) <= cfg.analysis.max_frames
    assert not (cfg.media_dir / "work").exists() or not any((cfg.media_dir / "work").iterdir())

    import app.main as main
    monkeypatch.setattr(main, "cfg", cfg)
    page = TestClient(main.app).get(f"/video/{vid}")
    assert page.status_code == 200
    for text in ("Why it outperformed", "Retention techniques", "Replicate this format", "cuts / 10s"):
        assert text in page.text
    frame = TestClient(main.app).get(f"/frame/{a['frames'][0]}", params={"v": vid})
    assert frame.status_code == 200 and frame.headers["content-type"] == "image/jpeg"


def test_upload_path_and_download_errors(cfg, tmp_path):
    with connect(cfg.db_path) as conn:
        conn.execute("INSERT INTO channels (id, platform, followers, median_views_short) "
                     "VALUES ('youtube:UC9', 'youtube', 5000, 2000)")
        conn.execute("INSERT INTO videos (id, platform, channel_id, title, published_at, duration_s, views, url) "
                     "VALUES ('youtube:abc', 'youtube', 'youtube:UC9', 'Real video', '2026-10-01T00:00:00Z', 30, "
                     "90000, 'https://www.youtube.com/shorts/abc')")
    clip = media.make_demo_clip(tmp_path / "up.mp4")
    pipeline.queue(cfg, "youtube:abc")
    pipeline.run_analysis(cfg, "youtube:abc", upload=clip, transcriber=FakeTranscriber(), analyzer=FakeAnalyzer())
    assert pipeline.get_analysis(cfg, "youtube:abc")["status"] == "done"
    assert not clip.exists()  # uploads are deleted after analysis

    cfg.analysis.allow_downloads = False
    pipeline.queue(cfg, "youtube:abc")
    pipeline.run_analysis(cfg, "youtube:abc", transcriber=FakeTranscriber(), analyzer=FakeAnalyzer())
    a = pipeline.get_analysis(cfg, "youtube:abc")
    assert a["status"] == "failed" and "Upload the file" in a["error"]


# --- the Claude request ---------------------------------------------------------

def _fake_breakdown():
    return {k: v for k, v in FakeAnalyzer().analyze({"title": "t"}, {"duration_s": 20}, {}, "", []).items()
            if k not in ("overall_score", "_meta")}


def _claude(handler, model="claude-opus-5-5"):
    client = anthropic.Anthropic(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handler)),
                                 max_retries=0)
    return ClaudeAnalyzer(model, "high", client=client)


def _message(text, stop="end_turn"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": text}] if text else [], "stop_reason": stop,
            "stop_sequence": None, "usage": {"input_tokens": 5000, "output_tokens": 1800,
                                             "cache_read_input_tokens": 1200}}


def test_claude_request_shape_and_parse(tmp_path):
    seen = {}

    def handler(req):
        seen["body"], seen["headers"] = json.loads(req.content), req.headers
        return httpx.Response(200, json=_message(json.dumps(_fake_breakdown())))

    clip = media.make_demo_clip(tmp_path / "c.mp4")
    frames = media.extract_frames(clip, [0.3, 5.0], tmp_path / "f")
    out = _claude(handler).analyze({"views": 1}, {"duration_s": 24}, {"words": 10}, "[000.0–002.4] Hi", frames)

    b = seen["body"]
    assert b["model"] == "claude-opus-5-5" and b["max_tokens"] == 16000
    assert b["thinking"] == {"type": "adaptive"}
    assert b["output_config"]["effort"] == "high"
    assert b["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
    assert b["system"][0]["text"] == SYSTEM_PROMPT and b["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert b["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in seen["headers"]["anthropic-beta"]
    content = b["messages"][0]["content"]
    assert content[0]["text"].startswith("<video_stats>") and "<transcript>" in content[0]["text"]
    assert sum(1 for c in content if c["type"] == "image") == 2
    assert content[-1]["type"] == "text" and "Break this video down" in content[-1]["text"]

    assert set(out["scores"]) == {k for k, _, _ in SCORE_CRITERIA} and out["overall_score"]
    assert out["_meta"]["cache_read_tokens"] == 1200


def test_schema_is_strict_everywhere():
    def walk(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False and set(node["required"]) == set(node["properties"])
            for child in node["properties"].values():
                walk(child)
        if node.get("type") == "array":
            walk(node["items"])
    walk(SCHEMA)


def test_claude_refusal_and_models_without_fallbacks():
    refusal = _message(None, stop="refusal")
    refusal["stop_details"] = {"type": "refusal", "category": "cyber", "explanation": "x"}
    with pytest.raises(AnalysisError, match="declined"):
        _claude(lambda r: httpx.Response(200, json=refusal)).analyze({}, {}, {}, "", [])

    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=_message(json.dumps(_fake_breakdown())))

    _claude(handler, model="claude-haiku-5-5").analyze({}, {}, {}, "", [])
    assert "fallbacks" not in seen["body"]

    with pytest.raises(AnalysisError, match="API key"):
        _claude(lambda r: httpx.Response(401, json={"type": "error", "error": {
            "type": "authentication_error", "message": "bad"}})).analyze({}, {}, {}, "", [])
