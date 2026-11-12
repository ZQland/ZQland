"""Command line for scheduled jobs.

    python -m app.cli refresh                 # refresh tracked creators (daily follower snapshots)
    python -m app.cli scan --genre fitness    # scan one genre on all searchable platforms
    python -m app.cli scan --genre all --platform tiktok

Schedule `refresh` once a day (cron, Task Scheduler, or a hosted cron) so the app
builds the follower history it needs to estimate followers gained per video.
"""
from __future__ import annotations

import argparse
import logging

from .config import load_config
from .db import init_db
from .models import PLATFORMS
from .providers import all_sources
from .queries import recent_scans
from .scanner import refresh_creators, run_scan, start_scan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("refresh", help="refresh tracked creators and take follower snapshots")
    scan = sub.add_parser("scan", help="scan keywords for outliers")
    scan.add_argument("--genre", default="all")
    scan.add_argument("--platform", action="append", choices=PLATFORMS)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per HTTP call is too noisy
    cfg = load_config()
    init_db(cfg.db_path)
    if args.cmd == "refresh":
        refresh_creators(cfg, start_scan(cfg, "refresh tracked creators (cli)"))
    else:
        genres = list(cfg.genres) if args.genre == "all" else [args.genre]
        platforms = args.platform or [s.platform for s in all_sources(cfg)
                                      if s.provider is not None and s.provider.can_search]
        run_scan(cfg, start_scan(cfg, f"{args.genre} on {', '.join(platforms)} (cli)"), genres, platforms)
    last = recent_scans(cfg, 1)[0]
    print(f"{last['status']}: {last['message']}")
    return 0 if last["status"] in ("finished", "partial") else 1


if __name__ == "__main__":
    raise SystemExit(main())
