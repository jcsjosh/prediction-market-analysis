"""Empirical calibration curves: how often a contract bought at a given price actually wins.

A curve maps ``(side, role, price)`` to a win probability, where ``side`` is the contract
bought (``yes``/``no``), ``role`` is whether the buyer was the ``taker`` or ``maker`` and
``price`` is 1-99 cents. The maker curve matters because a resting order only fills when
someone else chooses to trade against it.

Two sources:

* ``CalibrationTable.prior()`` - a conservative parametric curve shaped after published
  Kalshi findings (favourite-longshot bias, takers underperform makers, YES buyers
  underperform NO buyers). Use it only until you build a real table.
* ``build_from_dataset()`` - fits the curves on resolved trades from the
  prediction-market-analysis dataset (``make setup``), optionally per category group.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

SIDES = ("yes", "no")
ROLES = ("taker", "maker")
KEYS = tuple(f"{s}_{r}" for s in SIDES for r in ROLES)
ALL = "ALL"

# logit(q) = SLOPE * logit(p) + OFFSET[key]. SLOPE > 1 pushes longshots down and
# favourites up (favourite-longshot bias); offsets encode the taker/YES penalty.
PRIOR_SLOPE = 1.06
PRIOR_OFFSETS = {"yes_taker": -0.04, "no_taker": -0.01, "yes_maker": 0.0, "no_maker": 0.02}


def _logit(p: float) -> float:
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def prior_win_prob(key: str, price_cents: float) -> float:
    p = min(max(price_cents / 100.0, 0.005), 0.995)
    return _sigmoid(PRIOR_SLOPE * _logit(p) + PRIOR_OFFSETS[key])


@dataclass
class CalibrationTable:
    """Win-probability curves keyed by group (``ALL`` or a category group) then side_role."""

    source: str
    curves: dict[str, dict[str, list[float]]]
    counts: dict[str, dict[str, list[int]]] = field(default_factory=dict)

    @classmethod
    def prior(cls) -> CalibrationTable:
        curve = {k: [prior_win_prob(k, c) for c in range(1, 100)] for k in KEYS}
        return cls(source="prior", curves={ALL: curve})

    @classmethod
    def load(cls, path: Path | str) -> CalibrationTable:
        data = json.loads(Path(path).read_text())
        return cls(source=data.get("source", str(path)), curves=data["curves"], counts=data.get("counts", {}))

    def save(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"source": self.source, "curves": self.curves, "counts": self.counts}))
        return path

    def win_prob(self, side: str, role: str, price_cents: float, group: str = ALL) -> float:
        """Interpolated win probability for buying ``side`` at ``price_cents``."""
        key = f"{side}_{role}"
        curve = self.curves.get(group, {}).get(key) or self.curves[ALL][key]
        x = min(max(price_cents, 1.0), 99.0)
        lo = int(math.floor(x))
        hi = min(lo + 1, 99)
        frac = x - lo
        return curve[lo - 1] * (1 - frac) + curve[hi - 1] * frac


def smooth_curve(
    wins: list[float], totals: list[float], prior: list[float], strength: float, window: int
) -> list[float]:
    """Pool neighbouring cents, then shrink toward ``prior`` with ``strength`` pseudo-contracts."""
    out = []
    for i in range(99):
        lo, hi = max(0, i - window), min(98, i + window)
        w = sum(wins[lo : hi + 1])
        n = sum(totals[lo : hi + 1])
        out.append((w + strength * prior[i]) / (n + strength))
    return out


def build_from_dataset(
    trades_dir: Path | str,
    markets_dir: Path | str,
    by_group: bool = True,
    min_group_contracts: int = 2_000_000,
    strength: float = 50_000.0,
    window: int = 1,
    since: str | None = None,
) -> CalibrationTable:
    """Fit calibration curves on resolved Kalshi trades.

    ``strength`` is measured in contracts: a price bucket needs far more than that many
    contracts before its raw win rate dominates the prior. Contracts within one market are
    highly correlated, so this is deliberately large.
    """
    import duckdb

    from src.analysis.kalshi.util.categories import get_group

    where_since = f"AND t.created_time >= TIMESTAMP '{since}'" if since else ""
    con = duckdb.connect()
    df = con.execute(
        f"""
        WITH resolved AS (
            SELECT
                regexp_extract(m.event_ticker, '^([A-Za-z0-9]+)', 1) AS prefix,
                t.taker_side, t.yes_price, t.no_price, t.count, m.result
            FROM '{Path(trades_dir)}/*.parquet' t
            JOIN '{Path(markets_dir)}/*.parquet' m ON t.ticker = m.ticker
            WHERE m.result IN ('yes', 'no') AND t.yes_price BETWEEN 1 AND 99 {where_since}
        ),
        legs AS (
            SELECT prefix, 'yes' AS side, CASE WHEN taker_side = 'yes' THEN 'taker' ELSE 'maker' END AS role,
                   yes_price AS price, count, (result = 'yes') AS won FROM resolved
            UNION ALL
            SELECT prefix, 'no' AS side, CASE WHEN taker_side = 'no' THEN 'taker' ELSE 'maker' END AS role,
                   no_price AS price, count, (result = 'no') AS won FROM resolved
        )
        SELECT prefix, side, role, price,
               SUM(count)::DOUBLE AS n, SUM(CASE WHEN won THEN count ELSE 0 END)::DOUBLE AS w
        FROM legs GROUP BY ALL
        """
    ).df()

    if df.empty:
        raise ValueError("No resolved trades found - is the dataset downloaded (make setup)?")

    df["group"] = df["prefix"].map(lambda p: get_group(p or ""))
    prior = CalibrationTable.prior().curves[ALL]
    groups = [ALL] + (sorted(df["group"].unique()) if by_group else [])

    curves: dict[str, dict[str, list[float]]] = {}
    counts: dict[str, dict[str, list[int]]] = {}
    for group in groups:
        sub = df if group == ALL else df[df["group"] == group]
        if group != ALL and sub["n"].sum() < min_group_contracts:
            continue
        agg = sub.groupby(["side", "role", "price"])[["n", "w"]].sum()
        # Groups shrink toward the pooled curve rather than the parametric prior.
        base = curves.get(ALL, prior)
        curves[group], counts[group] = {}, {}
        for key in KEYS:
            side, role = key.split("_")
            wins, totals = [0.0] * 99, [0.0] * 99
            if (side, role) in agg.index.droplevel(2):
                part = agg.loc[(side, role)]
                for price, row in part.iterrows():
                    wins[int(price) - 1] = float(row["w"])
                    totals[int(price) - 1] = float(row["n"])
            curves[group][key] = smooth_curve(wins, totals, base[key], strength, window)
            counts[group][key] = [int(t) for t in totals]

    return CalibrationTable(source=f"dataset:{trades_dir}", curves=curves, counts=counts)
