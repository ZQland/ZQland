"""SQLite storage. One connection per call so background threads are safe."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY,              -- "<platform>:<native id or username>"
    platform TEXT NOT NULL,
    handle TEXT,
    title TEXT,
    url TEXT,
    followers INTEGER,                -- NULL when hidden
    median_views_short REAL,
    median_views_long REAL,
    sample_size INTEGER,
    tracked INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS videos (
    id TEXT PRIMARY KEY,              -- "<platform>:<native id>"
    platform TEXT NOT NULL,
    channel_id TEXT REFERENCES channels(id),
    title TEXT,
    description TEXT,
    published_at TEXT,
    duration_s INTEGER,
    views INTEGER,
    likes INTEGER,
    comments INTEGER,
    shares INTEGER,
    saves INTEGER,
    thumbnail TEXT,
    url TEXT,
    media_url TEXT,
    fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS videos_channel ON videos(channel_id);
CREATE TABLE IF NOT EXISTS hits (
    video_id TEXT REFERENCES videos(id),
    genre TEXT,
    keyword TEXT,
    PRIMARY KEY (video_id, genre, keyword)
);
CREATE TABLE IF NOT EXISTS keyword_stats (
    platform TEXT,
    genre TEXT,
    keyword TEXT,
    scanned_at TEXT,
    supply INTEGER,
    candidates INTEGER,
    outliers INTEGER,
    median_outlier REAL,
    opportunity REAL,
    PRIMARY KEY (platform, genre, keyword)
);
CREATE TABLE IF NOT EXISTS follower_snapshots (
    channel_id TEXT REFERENCES channels(id),
    taken_at TEXT,
    followers INTEGER,
    PRIMARY KEY (channel_id, taken_at)
);
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT,
    started_at TEXT,
    finished_at TEXT,
    status TEXT,
    quota_used INTEGER DEFAULT 0,
    message TEXT
);
CREATE TABLE IF NOT EXISTS analyses (
    video_id TEXT PRIMARY KEY REFERENCES videos(id),
    status TEXT,                      -- queued | running | done | failed
    step TEXT,
    error TEXT,
    transcript_json TEXT,
    metrics_json TEXT,
    breakdown_json TEXT,
    model TEXT,
    demo INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);
"""

_OLD_TABLES = ("hits", "keyword_stats", "videos", "channels", "scans")


def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        has_tables = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='videos'").fetchone()[0]
        if has_tables and version < SCHEMA_VERSION:
            # v1 only held cached public data; rebuild it rather than migrate.
            conn.execute("PRAGMA foreign_keys = OFF")
            for t in _OLD_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


@contextmanager
def connect(path: Path):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
