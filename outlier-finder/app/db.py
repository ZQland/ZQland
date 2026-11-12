"""SQLite storage. One connection per call so background threads are safe."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY,
    title TEXT,
    subscribers INTEGER,          -- NULL when the channel hides its count
    uploads_playlist TEXT,
    median_views_short REAL,
    median_views_long REAL,
    sample_size INTEGER,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS videos (
    id TEXT PRIMARY KEY,
    channel_id TEXT REFERENCES channels(id),
    title TEXT,
    description TEXT,
    published_at TEXT,
    duration_s INTEGER,
    views INTEGER,
    likes INTEGER,
    comments INTEGER,
    thumbnail TEXT,
    fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS hits (
    video_id TEXT REFERENCES videos(id),
    genre TEXT,
    keyword TEXT,
    PRIMARY KEY (video_id, genre, keyword)
);
CREATE TABLE IF NOT EXISTS keyword_stats (
    genre TEXT,
    keyword TEXT,
    scanned_at TEXT,
    supply INTEGER,
    candidates INTEGER,
    outliers INTEGER,
    median_outlier REAL,
    opportunity REAL,
    PRIMARY KEY (genre, keyword)
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
"""


def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def connect(path: Path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
