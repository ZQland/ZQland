"""Command line for scheduled jobs.

    python -m app.cli doctor                  # why does it say "demo data"? checks keys, .env, tools
    python -m app.cli refresh                 # check every tracked creator (daily follower snapshots)
    python -m app.cli scan --genre fitness    # check tracked creators in one genre, all platforms
    python -m app.cli scan --genre all --platform tiktok
    python -m app.cli scan --wide             # also search keywords (else: the switch on the Scans page)

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
    sub.add_parser("doctor", help="check .env, keys, data sources and tools")
    sub.add_parser("refresh", help="refresh tracked creators and take follower snapshots")
    scan = sub.add_parser("scan", help="check the creator list (and keywords with --wide) for outliers")
    scan.add_argument("--genre", default="all")
    scan.add_argument("--platform", action="append", choices=PLATFORMS)
    scan.add_argument("--wide", action="store_true", default=None,
                      help="also search keywords (default: the Wide scan switch on the Scans page)")
    args = parser.parse_args(argv)
    if args.cmd == "doctor":
        from .doctor import run
        print(run())
        return 0

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per HTTP call is too noisy
    cfg = load_config()
    init_db(cfg.db_path)
    if args.cmd == "refresh":
        refresh_creators(cfg, start_scan(cfg, "refresh tracked creators (cli)"))
    else:
        if args.genre != "all" and args.genre not in cfg.genres:
            parser.error(f"unknown genre {args.genre!r}; pick one of: all, {', '.join(cfg.genres)}")
        genres = None if args.genre == "all" else [args.genre]
        platforms = args.platform or [s.platform for s in all_sources(cfg) if s.provider is not None]
        run_scan(cfg, start_scan(cfg, f"{args.genre} on {', '.join(platforms)} (cli)"), genres, platforms,
                 wide=args.wide)
    last = recent_scans(cfg, 1)[0]
    print(f"{last['status']}: {last['message']}")
    if last.get("cost_usd"):
        print(f"Apify: ${last['cost_usd']:.2f}")
    return 0 if last["status"] in ("finished", "partial") else 1


if __name__ == "__main__":
    raise SystemExit(main())
