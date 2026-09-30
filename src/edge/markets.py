"""Fetch and normalise open Kalshi events and markets from the public API."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from dateutil.parser import isoparse

from src.analysis.kalshi.util.categories import get_group
from src.common.client import HttpClient
from src.indexers.kalshi.client import KALSHI_API_HOST


def _cents(data: dict, key: str) -> float | None:
    """Read a price in cents from ``<key>_dollars`` (current API) or ``<key>`` (legacy cents)."""
    raw = data.get(f"{key}_dollars")
    if raw not in (None, ""):
        value = float(raw) * 100.0
    elif data.get(key) not in (None, ""):
        value = float(data[key])
    else:
        return None
    return round(value, 2)


def _num(data: dict, key: str) -> float:
    raw = data.get(f"{key}_fp", data.get(key))
    try:
        return float(raw) if raw not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _time(val: Any) -> datetime | None:
    if not val:
        return None
    try:
        return isoparse(val)
    except (TypeError, ValueError):
        return None


def _tradeable(price: float | None) -> float | None:
    """Asks of 0/100 (or bids of 0) mean an empty book side."""
    if price is None or price <= 0 or price >= 100:
        return None
    return price


def _size(data: dict, *keys: str) -> float | None:
    for key in keys:
        raw = data.get(f"{key}_fp", data.get(key))
        if raw not in (None, ""):
            try:
                return float(raw)
            except (TypeError, ValueError):
                continue
    return None


def _underlying(m: dict) -> str:
    """Stable id for the quantity a strike market is written on.

    Kalshi tickers end in a strike segment that may carry a team code (``...-BSU3`` and
    ``...-USU2`` are different teams' spreads), and ``custom_strike`` names the team or
    player. Markets on the same ladder share the ticker stem, the segment's letters and
    the ``custom_strike``, and settle at the same time.
    """
    stem, _, last = m.get("ticker", "").rpartition("-")
    letters = re.sub(r"[-\d.]+$", "", last)
    custom = json.dumps(m.get("custom_strike") or {}, sort_keys=True)
    # Different deadlines are different questions (e.g. "by 2025" vs "by 2030").
    return f"{stem}|{letters}|{custom}|{m.get('close_time', '')}"


@dataclass
class Quote:
    ticker: str
    event_ticker: str
    title: str
    subtitle: str
    group: str
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    last_price: float | None
    volume: float
    volume_24h: float
    open_interest: float
    close_time: datetime | None
    strike_type: str = ""
    floor_strike: float | None = None
    cap_strike: float | None = None
    underlying: str = ""  # identifies one ladder when an event holds several (team, player)
    yes_ask_size: float | None = None  # contracts offered at the YES ask
    no_ask_size: float | None = None  # contracts offered at the NO ask (= resting YES bids)

    @classmethod
    def from_api(cls, m: dict, group: str = "") -> Quote:
        yes_bid = _tradeable(_cents(m, "yes_bid"))
        yes_ask = _tradeable(_cents(m, "yes_ask"))
        no_bid = _tradeable(_cents(m, "no_bid"))
        no_ask = _tradeable(_cents(m, "no_ask"))
        # A YES bid is a NO offer and vice versa; fill gaps from the opposite book.
        if no_ask is None and yes_bid is not None:
            no_ask = round(100 - yes_bid, 2)
        if yes_ask is None and no_bid is not None:
            yes_ask = round(100 - no_bid, 2)
        if no_bid is None and yes_ask is not None:
            no_bid = _tradeable(round(100 - yes_ask, 2))
        if yes_bid is None and no_ask is not None:
            yes_bid = _tradeable(round(100 - no_ask, 2))
        event_ticker = m.get("event_ticker", "")
        return cls(
            ticker=m["ticker"],
            event_ticker=event_ticker,
            title=m.get("title", ""),
            subtitle=m.get("yes_sub_title") or m.get("subtitle", ""),
            group=group or get_group(event_ticker),
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            last_price=_tradeable(_cents(m, "last_price")),
            volume=_num(m, "volume"),
            volume_24h=_num(m, "volume_24h"),
            open_interest=_num(m, "open_interest"),
            close_time=_time(m.get("close_time")),
            strike_type=m.get("strike_type", "") or "",
            floor_strike=m.get("floor_strike"),
            cap_strike=m.get("cap_strike"),
            underlying=_underlying(m),
            yes_ask_size=_size(m, "yes_ask_size") if yes_ask is not None else None,
            no_ask_size=_size(m, "no_ask_size", "yes_bid_size") if no_ask is not None else None,
        )

    @property
    def spread(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return round(self.yes_ask - self.yes_bid, 2)

    def days_to_close(self, now: datetime | None = None) -> float | None:
        if self.close_time is None:
            return None
        now = now or datetime.now(timezone.utc)
        return max((self.close_time - now).total_seconds() / 86400.0, 0.0)


@dataclass
class Event:
    event_ticker: str
    title: str
    category: str
    mutually_exclusive: bool
    markets: list[Quote] = field(default_factory=list)

    @classmethod
    def from_api(cls, e: dict) -> Event:
        event_ticker = e.get("event_ticker", "")
        group = get_group(event_ticker)
        if group == "Other" and e.get("category"):
            group = e["category"]
        markets = [
            Quote.from_api({**m, "event_ticker": m.get("event_ticker") or event_ticker}, group)
            for m in e.get("markets") or []
            if m.get("status", "active") in ("active", "open")
        ]
        return cls(
            event_ticker=event_ticker,
            title=e.get("title", ""),
            category=e.get("category", ""),
            mutually_exclusive=bool(e.get("mutually_exclusive")),
            markets=markets,
        )


class EdgeClient:
    """Thin read-only client for the public Kalshi endpoints the scanner needs."""

    def __init__(self, host: str = KALSHI_API_HOST, rate_limit: float = 8):
        self.http = HttpClient(base_url=host, rate_limit=rate_limit)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.http.close()

    def iter_open_events(self, max_pages: int = 200, series_ticker: str | None = None) -> Iterator[Event]:
        cursor = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"status": "open", "with_nested_markets": "true", "limit": 200}
            if series_ticker:
                params["series_ticker"] = series_ticker
            if cursor:
                params["cursor"] = cursor
            data = self.http.get("/events", params=params)
            for raw in data.get("events", []):
                yield Event.from_api(raw)
            cursor = data.get("cursor")
            if not cursor:
                return
