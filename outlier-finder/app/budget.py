"""Apify spending guards.

Every Apify run asks a budget for an allowance first. The allowance is the smallest of:
  - what's left of this month's budget (niches.yaml `apify.monthly_budget_usd`), measured
    against Apify's own usage figure for the account, so runs from anywhere count;
  - what's left of the account's own monthly limit on Apify;
  - what's left of this scan's cap (`apify.max_usd_per_scan`).
The allowance is sent to Apify as the run's `maxTotalChargeUsd`, so Apify itself stops the
run there: a bug or a huge profile can't overspend.
"""
from __future__ import annotations

import threading
import time

import httpx

APIFY = "https://api.apify.com/v2"
_MONTH_CACHE_SECONDS = 30
_month_cache: dict[str, tuple[float, dict | None]] = {}
_month_lock = threading.Lock()


class BudgetExceeded(RuntimeError):
    """Raised before an Apify run when the budget has no room for it."""


def apify_month(token: str | None, fresh: bool = False, http: httpx.Client | None = None) -> dict | None:
    """This billing cycle's Apify usage: {"used", "account_limit", "resets"}. None if unavailable."""
    if not token:
        return None
    with _month_lock:
        hit = _month_cache.get(token)
        if hit and not fresh and time.monotonic() - hit[0] < _MONTH_CACHE_SECONDS:
            return hit[1]
    try:
        client = http or httpx.Client(timeout=8)
        r = client.get(f"{APIFY}/users/me/limits", headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        d = r.json()["data"]
        info = {"used": float(d["current"]["monthlyUsageUsd"]),
                "account_limit": float(d["limits"]["maxMonthlyUsageUsd"]),
                "resets": (d.get("monthlyUsageCycle") or {}).get("endAt", "")[:10]}
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        info = None
    with _month_lock:
        _month_cache[token] = (time.monotonic(), info)
    return info


def apify_paused(cfg) -> str | None:
    """Why Apify platforms can't run right now, or None. Cached, so cheap to call per page."""
    m = month_summary(cfg) if cfg.apify_token else None
    if not m or m["left"] > 0.01:
        return None
    return (f"Apify budget used up: ${m['used']:.2f} of ${m['cap']:.2f} this month, resets {m['resets']}. "
            "Raise apify.monthly_budget_usd in niches.yaml (and the limit on Apify) to continue.")


def month_summary(cfg) -> dict | None:
    """For the Scans page: usage vs the effective cap (our budget or the account limit, whichever is lower)."""
    m = apify_month(cfg.apify_token)
    if not m:
        return None
    cap = min(cfg.apify.monthly_budget_usd, m["account_limit"])
    return {**m, "budget": cfg.apify.monthly_budget_usd, "cap": cap,
            "left": max(cap - m["used"], 0.0), "pct": min(100, round(100 * m["used"] / cap)) if cap else 100}


class ApifyBudget:
    """One per scan (shared by its platform threads), or a month-only guard for one-off runs."""

    def __init__(self, cfg, per_scan_usd: float | None = None, month=apify_month):
        self.cfg = cfg
        self.per_scan = per_scan_usd
        self.spent = 0.0
        self._lock = threading.Lock()
        self._month = month

    def allowance(self, minimum: float = 0.0) -> float:
        """USD the next run may spend. Raises BudgetExceeded when that's below `minimum` or ~0."""
        limits: list[tuple[float, str]] = []
        m = self._month(self.cfg.apify_token, fresh=True)
        if m:
            limits.append((self.cfg.apify.monthly_budget_usd - m["used"],
                           f"this month's Apify usage (${m['used']:.2f}) has reached your "
                           f"${self.cfg.apify.monthly_budget_usd:.2f} budget (apify.monthly_budget_usd)"))
            limits.append((m["account_limit"] - m["used"],
                           f"this month's Apify usage (${m['used']:.2f}) has reached the account's "
                           f"${m['account_limit']:.2f} limit on Apify"))
        if self.per_scan is not None:
            with self._lock:
                limits.append((self.per_scan - self.spent,
                               f"this scan has spent its ${self.per_scan:.2f} cap (apify.max_usd_per_scan)"))
        if not limits:
            return float("inf")
        left, why = min(limits, key=lambda x: x[0])
        if left < max(minimum, 0.01):
            resets = f" Resets {m['resets']}." if m and m.get("resets") and "month" in why else ""
            if left > 0.01:
                why = f"only ${left:.2f} of budget is left, below this scraper's ${minimum:.2f} minimum per run"
            raise BudgetExceeded(f"Apify paused: {why}.{resets}")
        return round(left, 4)

    def record(self, usd: float) -> None:
        with self._lock:
            self.spent += usd
