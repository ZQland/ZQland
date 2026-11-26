"""Speech-to-text with word timestamps (local faster-whisper), plus script metrics."""
from __future__ import annotations

import os
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Word:
    start: float
    end: float
    word: str


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)


@dataclass
class Transcript:
    language: str | None
    segments: list[Segment]

    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments).strip()

    def to_dict(self) -> dict:
        return {"language": self.language, "segments": [asdict(s) for s in self.segments]}

    @staticmethod
    def from_dict(d: dict) -> "Transcript":
        return Transcript(d.get("language"), [
            Segment(s["start"], s["end"], s["text"], [Word(**w) for w in s.get("words", [])])
            for s in d.get("segments", [])])

    def as_prompt_text(self) -> str:
        return "\n".join(f"[{s.start:05.1f}–{s.end:05.1f}] {s.text.strip()}" for s in self.segments)


def read_wav_16k(path: Path):
    """Read the 16 kHz mono 16-bit WAV that media.extract_audio writes, as float32 samples.

    Passing samples (not a file path) means faster-whisper skips its own PyAV decoding,
    which breaks when PyAV and faster-whisper versions drift apart."""
    import numpy as np
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise RuntimeError("Expected 16 kHz mono 16-bit WAV (use media.extract_audio).")
        data = w.readframes(w.getnframes())
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


class WhisperTranscriber:
    """faster-whisper on your own machine. The model downloads once on first use
    (from Hugging Face), then runs offline. Bigger models are more accurate but slower."""

    def __init__(self, model_size: str = "base"):
        self.model_size = model_size
        self._model = None

    def _load(self):
        if self._model is None:
            # Plain HTTPS downloads instead of Hugging Face's Xet protocol, which needs extra
            # hosts (cas-server.xethub.hf.co …) that locked-down networks often block.
            os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
            try:
                from faster_whisper import WhisperModel
            except ImportError as e:
                raise RuntimeError("faster-whisper is not installed: pip install faster-whisper") from e
            try:
                self._model = WhisperModel(self.model_size, device="auto", compute_type="int8")
            except Exception as e:
                raise RuntimeError(f"Couldn't load the Whisper '{self.model_size}' model "
                                   f"(first run downloads it from huggingface.co): {e}") from e
        return self._model

    def transcribe(self, audio_path: Path) -> Transcript:
        segments, info = self._load().transcribe(read_wav_16k(audio_path), word_timestamps=True, vad_filter=True)
        out = []
        for s in segments:
            words = [Word(round(float(w.start), 2), round(float(w.end), 2), w.word) for w in (s.words or [])]
            out.append(Segment(round(float(s.start), 2), round(float(s.end), 2), s.text, words))
        return Transcript(getattr(info, "language", None), out)


class FakeTranscriber:
    """Canned script for demo mode and tests."""

    LINES = [
        (0.0, 2.4, "Stop scrolling if you've been doing this wrong for years."),
        (2.4, 5.6, "I tested it for thirty days so you don't have to."),
        (5.6, 9.8, "Here's the first thing nobody tells you: most people skip the setup."),
        (9.8, 13.5, "But wait, the second one is where it actually gets interesting."),
        (13.5, 18.2, "Do this one change every day and watch what happens by week two."),
        (18.2, 21.5, "Results? Honestly better than I expected."),
        (21.5, 23.8, "Follow for part two, where I show the full routine."),
    ]

    def __init__(self, *_, **__):
        pass

    def transcribe(self, audio_path: Path) -> Transcript:
        segs = []
        for start, end, text in self.LINES:
            tokens = text.split()
            step = (end - start) / len(tokens)
            words = [Word(round(start + i * step, 2), round(start + (i + 1) * step, 2), " " + w)
                     for i, w in enumerate(tokens)]
            segs.append(Segment(start, end, " " + text, words))
        return Transcript("en", segs)


def script_metrics(t: Transcript, duration: float) -> dict:
    words = [w for s in t.segments for w in s.words]
    if not words:
        return {"words": 0, "words_per_minute": 0, "time_to_first_word_s": None, "pauses_over_0_7s": 0,
                "longest_pause_s": None, "words_in_first_3s": 0, "hook_text": "", "speech_coverage_pct": 0}
    gaps = [b.start - a.end for a, b in zip(words, words[1:])]
    spoken = sum(max(w.end - w.start, 0) for w in words)
    first3 = [w.word.strip() for w in words if w.start < 3.0]
    return {
        "words": len(words),
        "words_per_minute": round(len(words) / max(duration, 1) * 60),
        "time_to_first_word_s": round(words[0].start, 2),
        "pauses_over_0_7s": sum(1 for g in gaps if g > 0.7),
        "longest_pause_s": round(max(gaps), 2) if gaps else 0,
        "words_in_first_3s": len(first3),
        "hook_text": " ".join(first3),
        "speech_coverage_pct": round(min(spoken / max(duration, 0.1), 1) * 100),
    }
