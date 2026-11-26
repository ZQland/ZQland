"""Turn a breakdown into a plain verdict: what works in the video, what doesn't.

Scores come from Claude (1-10 per criterion, with evidence). Measured numbers (cuts, words per
minute, pauses) are checked against short-form rules of thumb so the page can flag them at a glance.
"""
from __future__ import annotations

from .analyzer import SCORE_CRITERIA, SCORE_WEIGHTS

WORKS_AT = 7    # 7-10: clearly above typical for the niche
FAILS_AT = 5    # 1-5: weak, missing or merely typical (it didn't help the video)

_LABELS = {k: (label, desc) for k, label, desc in SCORE_CRITERIA}


def verdict(breakdown: dict | None) -> dict:
    """{"works": [...], "fails": [...], "mixed": [...], "all": [...]}, each item
    {key, label, desc, score, evidence, weight}. Works sorted best first, fails worst first."""
    items = []
    for key, (label, desc) in _LABELS.items():
        s = ((breakdown or {}).get("scores") or {}).get(key) or {}
        if not s.get("score"):
            continue
        items.append({"key": key, "label": label, "desc": desc, "score": int(s["score"]),
                      "evidence": s.get("evidence", ""), "weight": SCORE_WEIGHTS.get(key, 1.0)})
    rank = lambda i: (i["score"], i["weight"])  # noqa: E731
    return {
        "works": sorted([i for i in items if i["score"] >= WORKS_AT], key=rank, reverse=True),
        "fails": sorted([i for i in items if i["score"] <= FAILS_AT], key=rank),
        "mixed": sorted([i for i in items if FAILS_AT < i["score"] < WORKS_AT], key=rank, reverse=True),
        "all": items,
    }


def tone(score: int | float | None) -> str:
    """CSS tone for a 1-10 score: good, mid or bad."""
    if score is None:
        return "none"
    return "good" if score >= WORKS_AT else ("bad" if score <= FAILS_AT else "mid")


# (key in metrics, label, unit, good test, bad test, rule of thumb shown on hover)
_RULES = [
    ("script", "time_to_first_word_s", "First word", "s", lambda v: v <= 0.5, lambda v: v > 1.5,
     "Speech should start within half a second."),
    ("script", "words_per_minute", "Words / min", "", lambda v: v >= 160, lambda v: v < 120,
     "Fast delivery (160+ wpm) holds attention; under 120 feels slow."),
    ("script", "pauses_over_0_7s", "Long pauses", "", lambda v: v <= 2, lambda v: v > 5,
     "Pauses over 0.7s are dead air; jump-cut them."),
    ("edit", "cuts_per_10s", "Cuts / 10s", "", lambda v: v >= 2, lambda v: v < 1,
     "Two or more visual changes every 10 seconds keeps it moving."),
    ("edit", "cuts_in_first_3s", "Cuts in hook", "", lambda v: v >= 1, lambda v: False,
     "A cut or visual change in the first 3 seconds helps stop the scroll."),
    ("edit", "longest_shot_s", "Longest shot", "s", lambda v: v <= 5, lambda v: v > 10,
     "A single shot over ~10 seconds risks a drop-off."),
    ("script", "speech_coverage_pct", "Speech coverage", "%", lambda v: v >= 80, lambda v: v < 50,
     "Share of the video with speech; low means silent stretches (fine for music-led videos)."),
]


def metric_checks(metrics: dict | None) -> list[dict]:
    """Measured numbers with a good / watch / bad flag each."""
    out = []
    for group, key, label, unit, good, bad, rule in _RULES:
        v = ((metrics or {}).get(group) or {}).get(key)
        if v is None:
            continue
        flag = "good" if good(v) else ("bad" if bad(v) else "mid")
        out.append({"label": label, "value": f"{v:g}{unit}", "flag": flag, "rule": rule})
    return out
