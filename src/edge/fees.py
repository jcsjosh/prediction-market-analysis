"""Kalshi trading fee model.

Kalshi charges ``ceil_to_cent(rate * C * P * (1 - P))`` per order, where ``C`` is the
contract count and ``P`` the price in dollars. Takers pay the general rate (7%); makers
pay nothing on most markets and 1.75% on the markets where maker fees apply. Some index
markets use a reduced taker rate (3.5%). Always confirm against Kalshi's current fee
schedule before trading.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

TAKER_RATE = 0.07
MAKER_RATE = 0.0
REDUCED_TAKER_PREFIXES = ("KXINX", "INX", "KXNASDAQ100", "NASDAQ100")
REDUCED_TAKER_RATE = 0.035


@dataclass(frozen=True)
class FeeSchedule:
    taker_rate: float = TAKER_RATE
    maker_rate: float = MAKER_RATE

    def rate(self, role: str, ticker: str = "") -> float:
        if role == "maker":
            return self.maker_rate
        if ticker.upper().startswith(REDUCED_TAKER_PREFIXES):
            return min(self.taker_rate, REDUCED_TAKER_RATE)
        return self.taker_rate

    def order_fee(self, count: int, price_cents: float, role: str, ticker: str = "") -> float:
        """Total fee in cents for an order of ``count`` contracts at ``price_cents``."""
        if count <= 0:
            return 0.0
        p = price_cents / 100.0
        dollars = self.rate(role, ticker) * count * p * (1.0 - p)
        # Round up to the next whole cent; the epsilon avoids 0.0700000001 -> 8 cents.
        return float(math.ceil(dollars * 100.0 - 1e-9))

    def per_contract(self, count: int, price_cents: float, role: str, ticker: str = "") -> float:
        """Average fee per contract in cents for an order of ``count`` contracts."""
        count = max(count, 1)
        return self.order_fee(count, price_cents, role, ticker) / count
