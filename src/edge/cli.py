"""Command-line entry point: ``uv run main.py edge <scan|calibrate|eval>``."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from src.edge.calibration import ALL, CalibrationTable, build_from_dataset
from src.edge.fees import FeeSchedule
from src.edge.markets import EdgeClient, Event
from src.edge.report import build_payload, format_arbs, format_table, write_outputs
from src.edge.strategy import ScanConfig, find_arbitrage, kelly_fraction, rank_ideas

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_OUT = ROOT / "output" / "edge"
DEFAULT_CAL = DEFAULT_OUT / "calibration.json"
COMBO_PREFIXES = ("KXMVE",)  # multivariate parlay events: thousands of thin, illiquid books


def load_calibration(path: str | None) -> CalibrationTable:
    target = Path(path) if path else DEFAULT_CAL
    if target.exists():
        return CalibrationTable.load(target)
    if path:
        sys.exit(f"Calibration file not found: {path}")
    print(
        "! Using the built-in PRIOR calibration curve. Run `uv run main.py edge calibrate` on the\n"
        "  downloaded dataset (make setup) for curves fitted to real Kalshi outcomes.\n",
        file=sys.stderr,
    )
    return CalibrationTable.prior()


def load_views(path: str | None) -> dict[str, float]:
    """CSV with ``ticker,prob`` rows; prob is your YES probability (0-1 or 0-100)."""
    if not path:
        return {}
    views = {}
    with open(path, newline="") as f:
        for row in csv.reader(f):
            if len(row) < 2 or row[0].strip().lower() == "ticker":
                continue
            prob = float(row[1])
            views[row[0].strip()] = prob / 100.0 if prob > 1 else prob
    return views


def cmd_scan(args: argparse.Namespace) -> None:
    cal = load_calibration(args.calibration)
    fees = FeeSchedule(taker_rate=args.taker_fee, maker_rate=args.maker_fee)
    cfg = ScanConfig(
        bankroll=args.bankroll,
        kelly_fraction=args.kelly,
        max_bet_fraction=args.max_bet,
        min_edge_cents=args.min_edge,
        min_volume_24h=args.min_volume,
        max_spread=args.max_spread,
        max_days=args.max_days if args.max_days > 0 else None,
        include_maker=not args.no_maker,
        views=load_views(args.views),
        view_weight=args.view_weight,
    )

    if args.from_file:
        raw = json.loads(Path(args.from_file).read_text())
        events = [Event.from_api(e) for e in raw]
    else:
        print("Fetching open Kalshi events…", file=sys.stderr)
        raw_events = []
        with EdgeClient() as client:
            for ev in client.iter_open_events(max_pages=args.max_pages, series_ticker=args.series):
                raw_events.append(ev)
        events = raw_events
    if not args.include_combos:
        events = [e for e in events if not e.event_ticker.upper().startswith(COMBO_PREFIXES)]

    quotes = [q for e in events for q in e.markets]
    ideas = rank_ideas(quotes, cal, fees, cfg)
    arbs = find_arbitrage(events, fees, cfg.arb_contracts)

    print(f"\nScanned {len(quotes)} markets in {len(events)} events · calibration: {cal.source}\n")
    print("ARBITRAGE (worst-case payout > cost, fees included)")
    print(format_arbs(arbs, args.top), "\n")
    plan = [i for i in ideas if i.in_plan]
    print(
        f"YOUR PLAN: {len(plan)} bets, ${sum(i.stake for i in plan):,.2f} staked of ${cfg.bankroll:,.0f}, "
        f"expected profit ${sum(i.expected_profit for i in plan):,.2f} "
        f"({cfg.kelly_fraction:g}× Kelly, one bet per event, * = your view). Top {args.top}:"
    )
    print(format_table(ideas, args.top))

    paths = write_outputs(build_payload(ideas, arbs, cal, cfg, len(quotes)), args.out)
    print(f"\nDashboard: {paths['html']}\nData: {paths['json']} · {paths['csv']}")


def cmd_calibrate(args: argparse.Namespace) -> None:
    data = ROOT / "data" / "kalshi"
    table = build_from_dataset(
        trades_dir=args.trades_dir or data / "trades",
        markets_dir=args.markets_dir or data / "markets",
        by_group=not args.no_groups,
        since=args.since,
        market_cap=args.market_cap,
    )
    path = table.save(args.out)
    prior = CalibrationTable.prior()
    print(f"Saved calibration ({', '.join(table.curves)}) to {path}\n")
    print(" price   YES taker   NO taker   YES maker   NO maker   (actual win % vs prior)")
    for c in (2, 5, 10, 20, 35, 50, 65, 80, 90, 95, 98):
        cells = []
        for key in ("yes_taker", "no_taker", "yes_maker", "no_maker"):
            side, role = key.split("_")
            cells.append(f"{table.win_prob(side, role, c) * 100:5.1f}/{prior.win_prob(side, role, c) * 100:4.1f}")
        print(f"  {c:>3}¢  " + "  ".join(f"{x:>10}" for x in cells))


def cmd_eval(args: argparse.Namespace) -> None:
    cal = load_calibration(args.calibration)
    fees = FeeSchedule(taker_rate=args.taker_fee, maker_rate=args.maker_fee)
    fee = fees.per_contract(args.contracts, args.price, args.role, args.ticker or "")
    base = cal.win_prob(args.side, args.role, args.price, args.group or ALL)
    prob = args.prob if args.prob is not None else base
    if prob > 1:
        prob /= 100.0
    cost = args.price + fee
    ev = 100 * prob - cost
    k = kelly_fraction(prob, cost)
    print(f"Buy {args.side.upper()} @ {args.price:g}¢ as {args.role} · fee {fee:.2f}¢/contract")
    print(f"  Calibration win rate at this price: {base * 100:.2f}%  (implied {args.price:g}%)")
    if args.prob is not None:
        print(f"  Your probability:                   {prob * 100:.2f}%")
    print(f"  EV per contract: {ev:+.2f}¢   ROI: {ev / cost * 100:+.2f}%")
    print(f"  Break-even win rate: {cost:.2f}%")
    if k > 0:
        stake = args.bankroll * min(k * args.kelly, args.max_bet)
        print(f"  Kelly: {k * 100:.2f}% full · stake ${stake:,.2f} ({args.kelly:g}× Kelly, capped {args.max_bet:.0%})")
    else:
        print("  Kelly: no bet (negative edge).")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="main.py edge", description="Find +EV Kalshi bets and arbitrage.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def money(sp):
        sp.add_argument("--bankroll", type=float, default=1000.0, help="dollars available (default 1000)")
        sp.add_argument("--kelly", type=float, default=0.25, help="fraction of full Kelly (default 0.25)")
        sp.add_argument("--max-bet", type=float, default=0.05, help="max fraction of bankroll per bet")
        sp.add_argument("--calibration", help=f"calibration JSON (default {DEFAULT_CAL.relative_to(ROOT)})")
        sp.add_argument("--taker-fee", type=float, default=0.07)
        sp.add_argument("--maker-fee", type=float, default=0.0)

    s = sub.add_parser("scan", help="scan open markets for +EV bets and arbitrage")
    money(s)
    s.add_argument("--min-edge", type=float, default=1.0, help="min EV per contract in cents after fees")
    s.add_argument("--min-volume", type=float, default=50, help="min contracts traded in 24h")
    s.add_argument("--max-spread", type=float, default=10, help="max bid/ask spread in cents")
    s.add_argument("--max-days", type=float, default=60, help="skip markets closing later (0 = no limit)")
    s.add_argument("--no-maker", action="store_true", help="only consider taking the ask")
    s.add_argument("--views", help="CSV of ticker,prob with your own YES probabilities")
    s.add_argument("--view-weight", type=float, default=1.0, help="weight on your view vs calibration")
    s.add_argument("--series", help="limit to one series ticker, e.g. KXHIGHNY")
    s.add_argument("--max-pages", type=int, default=200)
    s.add_argument("--include-combos", action="store_true", help="include multivariate parlay markets")
    s.add_argument("--from-file", help="score a saved /events JSON list instead of calling the API")
    s.add_argument("--top", type=int, default=25)
    s.add_argument("--out", default=str(DEFAULT_OUT))
    s.set_defaults(func=cmd_scan)

    c = sub.add_parser("calibrate", help="fit calibration curves on the downloaded dataset")
    c.add_argument("--trades-dir")
    c.add_argument("--markets-dir")
    c.add_argument("--since", help="only use trades after this date, e.g. 2025-01-01")
    c.add_argument("--no-groups", action="store_true", help="skip per-category curves")
    c.add_argument("--market-cap", type=int, default=1000, help="max contracts one event adds per price bucket")
    c.add_argument("--out", default=str(DEFAULT_CAL))
    c.set_defaults(func=cmd_calibrate)

    e = sub.add_parser("eval", help="evaluate one bet by hand")
    money(e)
    e.add_argument("--side", choices=["yes", "no"], required=True)
    e.add_argument("--price", type=float, required=True, help="price in cents")
    e.add_argument("--role", choices=["taker", "maker"], default="taker")
    e.add_argument("--prob", type=float, help="your win probability for this side (0-1 or 0-100)")
    e.add_argument("--contracts", type=int, default=100)
    e.add_argument("--group", help="category group for calibration, e.g. Sports")
    e.add_argument("--ticker", help="market ticker (applies reduced index fees)")
    e.set_defaults(func=cmd_eval)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)
