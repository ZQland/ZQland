"""Claude breakdown of one video: topic, format, sections, retention edits, scores."""
from __future__ import annotations

import base64
import json
from pathlib import Path

SCORE_CRITERIA = [
    ("hook", "Hook", "How fast the first 3 seconds create curiosity, stakes or a clear promise."),
    ("retention", "Retention & pacing", "Rehooks, open loops, cut rhythm, no dead air or slow stretches."),
    ("value", "Value", "Payoff for the viewer's time: useful, entertaining or emotionally rewarding."),
    ("payoff", "Payoff on the promise", "Whether it delivers what the hook promised, and when."),
    ("novelty", "Novelty / angle", "A fresh take or surprising framing versus typical videos on this topic."),
    ("emotion", "Emotional intensity", "Surprise, humor, awe, outrage, relief: how strongly it makes you feel."),
    ("shareability", "Shareability", "Would someone send it to a friend, save it, or argue about it in comments?"),
    ("clarity", "Clarity", "Easy to follow on first watch, including with the sound off."),
    ("cta", "Call to action", "Natural, specific and well-timed ask (follow, comment, part 2), or a smart loop."),
    ("replicability", "Replicability", "How easily another creator could make their own version of this format."),
]
SCORE_WEIGHTS = {"hook": 2.0, "retention": 1.5, "value": 1.5, "payoff": 1.0, "novelty": 1.0,
                 "emotion": 1.0, "shareability": 1.0, "clarity": 1.0, "cta": 0.5, "replicability": 0.5}

SECTION_TYPES = ["hook", "setup", "credibility", "rehook", "value_beat", "story", "twist",
                 "payoff", "proof", "cta", "loop", "other"]
HOOK_TYPES = ["question", "bold_claim", "curiosity_gap", "pattern_interrupt", "story_open", "pain_point",
              "result_first", "controversy", "tutorial_promise", "relatable_pov", "other"]

# Models that accept server-side refusal fallbacks (`fallbacks: "default"`).
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-2", "claude-fable-5-1"}


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


_S = {"type": "string"}
_N = {"type": "number"}

SCHEMA = _obj({
    "topic": {**_S, "description": "What the video is about, in a few words."},
    "angle": {**_S, "description": "The specific take or framing on the topic."},
    "format_name": {**_S, "description": "Short reusable name for the format, e.g. 'I tried X for 30 days'."},
    "format_description": _S,
    "target_viewer": _S,
    "hook": _obj({
        "spoken": {**_S, "description": "Exact words spoken in the first ~3 seconds ('' if none)."},
        "on_screen_text": {**_S, "description": "Text visible on screen in the first frames ('' if none)."},
        "visual": {**_S, "description": "What the viewer sees in the first seconds."},
        "hook_type": {**_S, "description": "One of: " + ", ".join(HOOK_TYPES) + "."},
        "promise": {**_S, "description": "What the hook implicitly promises the viewer will get."},
        "why_it_works": _S,
    }),
    "sections": {"type": "array", "items": _obj({
        "type": {"type": "string", "enum": SECTION_TYPES},
        "start_s": _N, "end_s": _N,
        "label": {**_S, "description": "A few words naming this section."},
        "script": {**_S, "description": "The words spoken in this section (verbatim or close)."},
        "purpose": {**_S, "description": "What this section does for retention or the viewer."},
    })},
    "retention_techniques": {"type": "array", "items": _obj({
        "technique": _S,
        "category": {"type": "string", "enum": ["script", "edit", "visual", "audio"]},
        "timestamp_s": _N,
        "evidence": {**_S, "description": "Quote or describe exactly what happens at that moment."},
    })},
    "edit_breakdown": _obj({
        "pacing_summary": _S,
        "caption_style": _S,
        "visual_style": _S,
        "audio_notes": _S,
        "notable_edits": {"type": "array", "items": _obj({"timestamp_s": _N, "edit": _S, "purpose": _S})},
    }),
    # A list (not one object per criterion) keeps the compiled output grammar small enough.
    "scores": {"type": "array", "description": "Exactly one entry per criterion, all 10 criteria.",
               "items": _obj({
                   "criterion": {"type": "string", "enum": [k for k, _, _ in SCORE_CRITERIA]},
                   "score": {"type": "integer", "description": "1 to 10."},
                   "evidence": {**_S, "description": "Why, citing timestamps or quotes."},
               })},
    "why_it_outperformed": {**_S, "description": "The 2–4 factors most likely behind the outlier performance."},
    "replicate": _obj({
        "script_template": {**_S, "description": "A fill-in-the-blanks script of this format."},
        "steps": {"type": "array", "items": _S},
        "topic_ideas": {"type": "array", "items": _S,
                        "description": "5 topics in the same niche this format would suit."},
    }),
})

_criteria_text = "\n".join(f"- {key} ({label}): {desc}" for key, label, desc in SCORE_CRITERIA)

SYSTEM_PROMPT = f"""You analyze short-form videos (YouTube Shorts, TikTok, Instagram Reels) that got far more views than their creator usually gets. The reader is a creator in the same niche who wants to understand why this video beat the creator's normal performance, so they can use the same topic, format and techniques in their own videos.

Be specific and grounded in the evidence you're given: cite timestamps and quote the script. Generic praise ("great hook", "engaging content") is useless to the reader; say exactly what the creator did and why it holds attention. If something is weak, say so. The video outperformed, but not every part of it is good, and knowing which parts carried it is the point.

<inputs>
You receive:
- <video_stats>: platform, views, the creator's median views, outlier score (views ÷ median), followers, likes, comments, shares, saves, engagement rate.
- <edit_metrics>: measured with ffmpeg scene detection. It catches hard cuts but can miss jump cuts within the same framing and can count fast camera moves as cuts, so treat the numbers as approximate and check them against the frames.
- <script_metrics>: words per minute, time to first word, pauses, words in the first 3 seconds.
- <transcript>: speech with [start–end] timestamps in seconds, from Whisper. It may contain small recognition errors; read through them. It may be empty for music-only or text-on-screen videos; then work from the frames and say that the video has no speech.
- Frames: still images sampled from the video, each labeled with its timestamp. The first frames cover the hook; later frames are taken just after cuts. Read on-screen text and captions from them.
</inputs>

<sections>
Split the whole video into consecutive sections that cover it from 0 to the end. Section types:
hook (first seconds that stop the scroll), setup (context, who it's for), credibility (why trust the creator: results, experience), rehook (a line or moment that resets attention mid-video, often an open loop like "but here's the part nobody tells you"), value_beat (one distinct tip, step or point; list each separately), story (narrative beat), twist (surprise or escalation), payoff (where the hook's promise is delivered), proof (results shown), cta (an ask: follow, comment, part 2), loop (an ending that flows back into the start to encourage rewatching), other.
</sections>

<scoring>
Score each criterion from 1 to 10 for this video:
{_criteria_text}

Anchors: 1–3 means weak or missing, 4–6 means typical for the niche, 7–8 means clearly above typical, 9–10 means exceptional and a main reason the video outperformed. Use the full range. These scores are later checked against how far each video actually outperformed, so inflated scores make the tool useless. A video can be a big outlier with a mediocre CTA.
</scoring>

<replicate>
Write a script template that keeps this video's structure and techniques but has blanks for the topic, so the reader can reuse the format. Suggest topics in the same niche where this format would fit.
</replicate>

All timestamps are in seconds from the start of the video."""


class AnalysisError(RuntimeError):
    pass


def overall_score(scores: dict) -> float | None:
    vals = [(SCORE_WEIGHTS[k], v["score"]) for k, v in scores.items() if k in SCORE_WEIGHTS and v.get("score")]
    if not vals:
        return None
    return round(sum(w * s for w, s in vals) / sum(w for w, _ in vals), 1)


def _clean(data: dict) -> dict:
    scores = data.get("scores") or {}
    if isinstance(scores, list):  # API returns a list; the app works with {criterion: {...}}
        scores = {s["criterion"]: {"score": s.get("score"), "evidence": s.get("evidence", "")}
                  for s in scores if s.get("criterion")}
    for key, _, _ in SCORE_CRITERIA:  # a missing criterion shows as unscored, not a crash
        scores.setdefault(key, {"score": None, "evidence": "Not scored."})
    for v in scores.values():
        if v.get("score") is not None:
            v["score"] = max(1, min(10, int(v["score"])))
    data["scores"] = scores
    data["sections"] = sorted(data.get("sections", []), key=lambda s: s.get("start_s", 0))
    data["overall_score"] = overall_score(data.get("scores", {}))
    return data


def build_user_content(stats: dict, edit: dict, script: dict, transcript_text: str,
                       frames: list[tuple[float, Path]]) -> list[dict]:
    """Long data first, frames next, the instruction last."""
    content: list[dict] = [{"type": "text", "text": (
        f"<video_stats>\n{json.dumps(stats, indent=1)}\n</video_stats>\n\n"
        f"<edit_metrics>\n{json.dumps(edit, indent=1)}\n</edit_metrics>\n\n"
        f"<script_metrics>\n{json.dumps(script, indent=1)}\n</script_metrics>\n\n"
        f"<transcript>\n{transcript_text or '(no speech detected)'}\n</transcript>")}]
    for t, path in frames:
        content.append({"type": "text", "text": f"Frame at {t:.1f}s:"})
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg",
            "data": base64.standard_b64encode(path.read_bytes()).decode("ascii")}})
    content.append({"type": "text", "text": (
        "Break this video down: its topic and angle, reusable format, hook, every section with "
        "timestamps, the script and editing techniques that keep viewers watching, scores with "
        "evidence, why it outperformed, and how to replicate the format.")})
    return content


class ClaudeAnalyzer:
    demo = False

    def __init__(self, model: str = "claude-opus-5-5", effort: str = "high", client=None):
        import anthropic  # imported here so demo mode works without the package configured
        self.anthropic = anthropic
        self.client = client or anthropic.Anthropic()
        self.model, self.effort = model, effort

    def analyze(self, stats: dict, edit: dict, script: dict, transcript_text: str,
                frames: list[tuple[float, Path]]) -> dict:
        a = self.anthropic
        kwargs = dict(
            model=self.model,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort,
                           "format": {"type": "json_schema", "schema": SCHEMA}},
            # The system prompt is identical for every video, so it's cached.
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": build_user_content(stats, edit, script, transcript_text, frames)}],
        )
        if self.model in FALLBACK_MODELS:
            # If a safety classifier declines, retry server-side on Anthropic's recommended model.
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        try:
            resp = self.client.beta.messages.create(**kwargs)
        except a.AuthenticationError as e:
            raise AnalysisError("Anthropic rejected the API key. Check ANTHROPIC_API_KEY.") from e
        except a.RateLimitError as e:
            raise AnalysisError("Rate limited by the Anthropic API. Try again in a minute.") from e
        except a.BadRequestError as e:
            raise AnalysisError(f"Anthropic API rejected the request: {e.message}") from e
        except a.APIStatusError as e:
            raise AnalysisError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except a.APIConnectionError as e:
            raise AnalysisError("Couldn't reach the Anthropic API.") from e

        if resp.stop_reason == "refusal":
            cat = getattr(getattr(resp, "stop_details", None), "category", None)
            raise AnalysisError(f"Claude declined to analyze this video (category: {cat}).")
        if resp.stop_reason == "max_tokens":
            raise AnalysisError("The analysis ran out of output tokens. Try a shorter video or fewer frames.")
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if not text:
            raise AnalysisError("Claude returned no analysis text.")
        data = _clean(json.loads(text))
        u = resp.usage
        data["_meta"] = {"model": resp.model, "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                         "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0}
        return data


class FakeAnalyzer:
    """Deterministic, clearly labeled stand-in used when no Anthropic key is set."""
    demo = True

    def analyze(self, stats, edit, script, transcript_text, frames) -> dict:
        title = stats.get("title") or "this topic"
        dur = edit.get("duration_s") or 24
        hook = script.get("hook_text") or "Stop scrolling if you've been doing this wrong"
        sections = [
            ("hook", 0.0, 2.4, "Scroll-stopper", hook, "Calls out a mistake the viewer might be making."),
            ("credibility", 2.4, 5.6, "Proof of effort", "I tested it for thirty days so you don't have to.",
             "Borrowed authority: the creator did the work."),
            ("value_beat", 5.6, 9.8, "Tip 1", "Most people skip the setup.", "First concrete takeaway."),
            ("rehook", 9.8, 13.5, "Open loop", "The second one is where it gets interesting.",
             "Resets attention before the midpoint drop-off."),
            ("value_beat", 13.5, 18.2, "Tip 2", "Do this one change every day.", "Main payoff tip."),
            ("payoff", 18.2, 21.5, "Result", "Better than I expected.", "Delivers the hook's promise."),
            ("cta", 21.5, dur, "Part 2 ask", "Follow for part two.", "Turns interest into a follow."),
        ]
        scores = {k: {"score": s, "evidence": f"Demo evidence for {label.lower()}."}
                  for (k, label, _), s in zip(SCORE_CRITERIA, [8, 7, 6, 7, 5, 6, 6, 8, 7, 9])}
        return _clean({
            "topic": title[:60], "angle": "Tested it myself so you don't have to",
            "format_name": "I tried X for 30 days",
            "format_description": "Creator runs a personal experiment and reports the 2–3 lessons that mattered.",
            "target_viewer": "Beginners who've tried and stalled",
            "hook": {"spoken": hook, "on_screen_text": "DON'T DO THIS", "visual": "Close-up, fast zoom-in",
                     "hook_type": "pain_point", "promise": "You'll learn the mistake and the fix.",
                     "why_it_works": "Calls out the viewer directly and implies they're losing out."},
            "sections": [{"type": t, "start_s": a, "end_s": b, "label": lab, "script": sc, "purpose": pu}
                         for t, a, b, lab, sc, pu in sections],
            "retention_techniques": [
                {"technique": "Open loop", "category": "script", "timestamp_s": 9.8,
                 "evidence": "\"the second one is where it actually gets interesting\""},
                {"technique": "Cut on every sentence", "category": "edit", "timestamp_s": 1.2,
                 "evidence": f"{edit.get('cuts_per_10s', 0)} cuts per 10 seconds"},
                {"technique": "Part-2 cliffhanger", "category": "script", "timestamp_s": 21.5,
                 "evidence": "\"Follow for part two\""},
            ],
            "edit_breakdown": {
                "pacing_summary": f"About {edit.get('avg_shot_s', '?')}s per shot, faster in the hook.",
                "caption_style": "Bold word-by-word captions, centered.", "visual_style": "Talking head + B-roll",
                "audio_notes": "Light background track under voice.",
                "notable_edits": [{"timestamp_s": c, "edit": "Hard cut", "purpose": "Keeps momentum"}
                                  for c in (edit.get("cut_times_s") or [])[:4]]},
            "scores": scores,
            "why_it_outperformed": "Demo analysis: a pain-point hook plus a mid-video open loop.",
            "replicate": {"script_template": "Stop scrolling if you [common mistake]. I tried [X] for [time]. "
                                             "First, [tip 1]. But the second one is where it gets interesting: "
                                             "[tip 2]. Result: [result]. Follow for part two.",
                          "steps": ["Pick a mistake your audience makes", "Test the fix yourself",
                                    "Open a loop before the midpoint", "End on a part-2 ask"],
                          "topic_ideas": ["Demo idea 1", "Demo idea 2", "Demo idea 3"]},
            "_meta": {"model": "demo (no Anthropic key)"},
        })
