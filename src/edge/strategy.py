"""Turn quotes into ranked bet ideas and risk-free arbitrage baskets."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from itertools import combinations

from src.edge.calibration import CalibrationTable
from src.edge.fees import FeeSchedule
from src.edge.markets import Event, Quote


@dataclass
class ScanConfig:
    bankroll: float = 1000.0  # dollars
    kelly_fraction: float = 0.25  # fraction of full Kelly to stake
    max_bet_fraction: float = 0.05  # hard cap per idea, as a fraction of bankroll
    min_edge_cents: float = 1.0  # required EV per contract after fees
    min_volume_24h: float = 50.0
    max_spread: float = 10.0
    min_price: float = 2.0
    max_price: float = 98.0
    max_days: float | None = 60.0
    include_maker: bool = True
    liquidity_share: float = 0.10  # never size above this share of 24h volume
    arb_contracts: int = 100  # basket size used to price arbitrage fees
    views: dict[str, float] = field(default_factory=dict)  # ticker -> your YES probability
    view_weight: float = 1.0  # 1.0 = trust your view entirely, 0.5 = blend with calibration
    max_total_fraction: float = 1.0  # the plan never stakes more than this share of bankroll


@dataclass
class Idea:
    ticker: str
    event_ticker: str
    title: str
    subtitle: str
    group: str
    action: str  # e.g. "BUY NO @ 94 (take)"
    side: str
    role: str
    price: float
    fee: float  # cents per contract at the suggested size
    win_prob: float
    implied_prob: float
    ev_cents: float  # expected profit per contract after fees
    roi: float  # ev / cost
    annualized: float | None
    kelly: float  # full-Kelly fraction of bankroll
    contracts: int
    stake: float  # dollars at risk
    expected_profit: float  # dollars
    days_to_close: float | None
    spread: float | None
    volume_24h: float
    source: str  # "calibration" or "your view"
    in_plan: bool = False  # chosen for the bankroll-limited plan

    def to_dict(self) -> dict:
        return asdict(self)


def kelly_fraction(win_prob: float, cost_cents: float) -> float:
    """Full-Kelly bankroll fraction for a contract costing ``cost_cents`` that pays 100."""
    c = cost_cents / 100.0
    if c <= 0 or c >= 1 or win_prob <= c:
        return 0.0
    return (win_prob - c) / (1.0 - c)


def _win_prob(
    q: Quote, side: str, role: str, price: float, cal: CalibrationTable, cfg: ScanConfig
) -> tuple[float, str]:
    p = cal.win_prob(side, role, price, q.group)
    if q.ticker in cfg.views:
        view_yes = min(max(cfg.views[q.ticker], 0.0), 1.0)
        view = view_yes if side == "yes" else 1.0 - view_yes
        return cfg.view_weight * view + (1 - cfg.view_weight) * p, "your view"
    return p, "calibration"


def _candidates(q: Quote, include_maker: bool) -> list[tuple[str, str, float]]:
    out = []
    for side in ("yes", "no"):
        ask = q.yes_ask if side == "yes" else q.no_ask
        bid = q.yes_bid if side == "yes" else q.no_bid
        if ask is not None:
            out.append((side, "taker", ask))
        # Improve the best bid by one cent, as long as that does not cross the ask.
        if include_maker and bid is not None and ask is not None and bid + 1 < ask:
            out.append((side, "maker", bid + 1))
    return out


def evaluate_quote(q: Quote, cal: CalibrationTable, fees: FeeSchedule, cfg: ScanConfig) -> list[Idea]:
    """Score every way of entering ``q`` and return the ones with positive edge."""
    days = q.days_to_close()
    ideas = []
    for side, role, price in _candidates(q, cfg.include_maker):
        if not (cfg.min_price <= price <= cfg.max_price):
            continue
        win_prob, source = _win_prob(q, side, role, price, cal, cfg)

        # Size first with a one-contract fee (worst rounding), then re-price fees at size.
        fee = fees.per_contract(1, price, role, q.ticker)
        full_kelly = kelly_fraction(win_prob, price + fee)
        stake_cap = cfg.bankroll * min(full_kelly * cfg.kelly_fraction, cfg.max_bet_fraction)
        contracts = int(stake_cap * 100 // (price + fee)) if full_kelly > 0 else 0
        if cfg.liquidity_share > 0 and q.volume_24h > 0:
            contracts = min(contracts, int(q.volume_24h * cfg.liquidity_share))
        if contracts > 0:
            fee = fees.per_contract(contracts, price, role, q.ticker)
            full_kelly = kelly_fraction(win_prob, price + fee)

        ev = 100.0 * win_prob - price - fee
        if ev < cfg.min_edge_cents or contracts <= 0:
            continue
        cost = price + fee
        roi = ev / cost
        annualized = None
        if days is not None and days > 0:
            annualized = math.expm1(math.log1p(roi) * min(365.0 / max(days, 1.0), 365.0))
        verb = "take" if role == "taker" else "post limit"
        ideas.append(
            Idea(
                ticker=q.ticker,
                event_ticker=q.event_ticker,
                title=q.title,
                subtitle=q.subtitle,
                group=q.group,
                action=f"BUY {side.upper()} @ {price:g}¢ ({verb})",
                side=side,
                role=role,
                price=price,
                fee=round(fee, 3),
                win_prob=round(win_prob, 4),
                implied_prob=round(price / 100.0, 4),
                ev_cents=round(ev, 3),
                roi=round(roi, 4),
                annualized=round(annualized, 4) if annualized is not None else None,
                kelly=round(full_kelly, 4),
                contracts=contracts,
                stake=round(contracts * cost / 100.0, 2),
                expected_profit=round(contracts * ev / 100.0, 2),
                days_to_close=round(days, 2) if days is not None else None,
                spread=q.spread,
                volume_24h=q.volume_24h,
                source=source,
            )
        )
    return ideas


def passes_filters(q: Quote, cfg: ScanConfig) -> bool:
    if q.ticker in cfg.views:
        return True  # always score markets you have an explicit view on
    if q.volume_24h < cfg.min_volume_24h:
        return False
    if q.spread is not None and q.spread > cfg.max_spread:
        return False
    days = q.days_to_close()
    return not (cfg.max_days is not None and days is not None and days > cfg.max_days)


def rank_ideas(quotes: list[Quote], cal: CalibrationTable, fees: FeeSchedule, cfg: ScanConfig) -> list[Idea]:
    """Best idea per event, ranked by expected dollar profit, with the plan marked.

    Markets in one event share an outcome (one game, one day's temperature), so only the
    best idea per event is kept. Walking down the ranking, ideas join the plan until the
    total stake would pass ``max_total_fraction`` of the bankroll.
    """
    best: dict[str, Idea] = {}
    for q in quotes:
        if not passes_filters(q, cfg):
            continue
        for idea in evaluate_quote(q, cal, fees, cfg):
            key = idea.event_ticker or idea.ticker
            cur = best.get(key)
            if cur is None or idea.expected_profit > cur.expected_profit:
                best[key] = idea
    ranked = sorted(best.values(), key=lambda i: (i.expected_profit, i.roi), reverse=True)
    budget = cfg.bankroll * cfg.max_total_fraction
    for idea in ranked:
        if idea.stake <= budget:
            idea.in_plan = True
            budget -= idea.stake
    return ranked


# --- Arbitrage -------------------------------------------------------------------------


@dataclass
class Arb:
    kind: str
    event_ticker: str
    title: str
    legs: list[dict]
    cost_cents: float  # per basket, fees included
    min_payout_cents: float
    profit_cents: float  # guaranteed profit per basket
    roi: float
    risk_free: bool
    note: str
    max_baskets: int | None = None  # limited by the thinnest leg's resting size

    def to_dict(self) -> dict:
        return asdict(self)


def _leg(q: Quote, side: str, price: float, fee: float) -> dict:
    return {"ticker": q.ticker, "subtitle": q.subtitle, "side": side, "price": price, "fee": round(fee, 3)}


def _basket(kind, event, legs_spec, payout, fees: FeeSchedule, n: int, risk_free: bool, note: str) -> Arb | None:
    sizes = [q.yes_ask_size if side == "yes" else q.no_ask_size for q, side, _ in legs_spec]
    max_baskets = int(min(sizes)) if sizes and all(s is not None for s in sizes) else None
    if max_baskets == 0:
        return None  # nothing resting at these prices
    # Fees round up per order, so price them at the size that can actually fill.
    n = min(n, max_baskets) if max_baskets is not None else n
    legs, cost = [], 0.0
    for q, side, price in legs_spec:
        fee = fees.per_contract(n, price, "taker", q.ticker)
        legs.append(_leg(q, side, price, fee))
        cost += price + fee
    profit = payout - cost
    if profit <= 0:
        return None
    return Arb(
        kind=kind,
        event_ticker=event.event_ticker,
        title=event.title,
        legs=legs,
        cost_cents=round(cost, 3),
        min_payout_cents=payout,
        profit_cents=round(profit, 3),
        roi=round(profit / cost, 4),
        risk_free=risk_free,
        note=note,
        max_baskets=max_baskets,
    )


def _ladders(rungs: list[Quote], key: str) -> list[list[Quote]]:
    """Split strike markets into ladders on the same underlying, sorted by strike.

    One event can hold several ladders (every pitcher's strikeout lines, each team's
    spread), so rungs are grouped on ``Quote.underlying``.
    """
    groups: dict[str, list[Quote]] = {}
    for q in rungs:
        groups.setdefault(q.underlying or q.ticker.rsplit("-", 1)[0], []).append(q)
    return [sorted(g, key=lambda q: float(getattr(q, key))) for g in groups.values()]


def find_arbitrage(events: list[Event], fees: FeeSchedule, contracts: int = 100) -> list[Arb]:
    """Baskets whose worst-case payout beats their all-in cost.

    * Mutually exclusive event, buy every NO: at most one market resolves YES, so at least
      ``n - 1`` NO contracts pay out. Risk-free.
    * Mutually exclusive event, buy every YES: pays exactly 100 only if the listed outcomes
      are exhaustive (someone must win). Flagged as not risk-free - check the rules.
    * Strike ladders: ``above X1`` must be at least as likely as ``above X2`` when X1 < X2.
      Buying YES(above X1) + NO(above X2) always pays at least 100. Same for ``below``.
    """
    arbs: list[Arb] = []
    for ev in events:
        ms = ev.markets
        if ev.mutually_exclusive and len(ms) >= 2:
            if all(q.no_ask is not None for q in ms):
                spec = [(q, "no", q.no_ask) for q in ms]
                arb = _basket(
                    "all-NO", ev, spec, 100.0 * (len(ms) - 1), fees, contracts, True, "At most one outcome can win."
                )
                if arb:
                    arbs.append(arb)
            if all(q.yes_ask is not None for q in ms):
                spec = [(q, "yes", q.yes_ask) for q in ms]
                arb = _basket(
                    "all-YES",
                    ev,
                    spec,
                    100.0,
                    fees,
                    contracts,
                    False,
                    "Only risk-free if these outcomes are exhaustive; confirm no 'none/other' result is possible.",
                )
                if arb:
                    arbs.append(arb)

        for kind, key, direction in (("above", "floor_strike", "greater"), ("below", "cap_strike", "less")):
            # A rung has only one bound; "less" markets with both bounds are single buckets.
            other = "cap_strike" if key == "floor_strike" else "floor_strike"
            rungs = [
                q
                for q in ms
                if q.strike_type.startswith(direction) and getattr(q, key) is not None and getattr(q, other) is None
            ]
            pairs = [pair for ladder in _ladders(rungs, key) for pair in combinations(ladder, 2)]
            for lo, hi in pairs:
                if float(getattr(lo, key)) == float(getattr(hi, key)):
                    continue
                # "above": YES on the lower strike, NO on the higher; "below": the reverse.
                yes_q, no_q = (lo, hi) if kind == "above" else (hi, lo)
                if yes_q.yes_ask is None or no_q.no_ask is None:
                    continue
                spec = [(yes_q, "yes", yes_q.yes_ask), (no_q, "no", no_q.no_ask)]
                arb = _basket(
                    f"ladder-{kind}",
                    ev,
                    spec,
                    100.0,
                    fees,
                    contracts,
                    True,
                    f"'{kind} {getattr(yes_q, key)}' is always at least as likely as '{kind} {getattr(no_q, key)}'.",
                )
                if arb:
                    arbs.append(arb)
    return sorted(arbs, key=lambda a: (a.risk_free, a.roi), reverse=True)
