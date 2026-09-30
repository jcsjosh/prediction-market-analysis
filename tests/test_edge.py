"""Tests for the Kalshi edge finder (src/edge)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.edge.calibration import ALL, KEYS, CalibrationTable, build_from_dataset
from src.edge.cli import main as edge_main
from src.edge.fees import FeeSchedule
from src.edge.markets import Event, Quote
from src.edge.report import build_payload, write_outputs
from src.edge.strategy import ScanConfig, evaluate_quote, find_arbitrage, kelly_fraction, rank_ideas

CLOSE = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()


def market(ticker="KXTEST-1", yes_bid=None, yes_ask=None, no_bid=None, no_ask=None, **extra) -> dict:
    m = {
        "ticker": ticker,
        "event_ticker": "KXTEST",
        "title": "Test",
        "status": "active",
        "close_time": CLOSE,
        "volume_24h_fp": "5000.00",
    }
    for key, val in (("yes_bid", yes_bid), ("yes_ask", yes_ask), ("no_bid", no_bid), ("no_ask", no_ask)):
        if val is not None:
            m[f"{key}_dollars"] = f"{val / 100:.4f}"
    m.update(extra)
    return m


# --- fees -----------------------------------------------------------------------------


def test_taker_fee_rounds_up_per_order():
    fees = FeeSchedule()
    assert fees.order_fee(1, 50, "taker") == 2  # 0.07 * 0.25 = 1.75c -> 2c
    assert fees.order_fee(100, 50, "taker") == 175
    assert fees.order_fee(100, 50, "maker") == 0


def test_reduced_fee_for_index_markets():
    assert FeeSchedule().order_fee(100, 50, "taker", "KXINXU-25") == pytest.approx(88)


# --- calibration ----------------------------------------------------------------------


def test_prior_has_longshot_bias_and_taker_penalty():
    cal = CalibrationTable.prior()
    assert cal.win_prob("yes", "taker", 5) < 0.05  # longshots win less than implied
    assert cal.win_prob("no", "maker", 95) > 0.95  # favourites win more
    for price in (10, 50, 90):
        assert cal.win_prob("yes", "taker", price) < cal.win_prob("yes", "maker", price)
        assert cal.win_prob("yes", "taker", price) < cal.win_prob("no", "taker", price)


def test_calibration_roundtrip_and_interpolation(tmp_path):
    cal = CalibrationTable.prior()
    loaded = CalibrationTable.load(cal.save(tmp_path / "cal.json"))
    mid = loaded.win_prob("yes", "taker", 40.5)
    assert loaded.win_prob("yes", "taker", 40) < mid < loaded.win_prob("yes", "taker", 41)
    assert loaded.win_prob("yes", "taker", 40, group="Missing") == loaded.win_prob("yes", "taker", 40)


def test_build_from_dataset(kalshi_trades_dir, kalshi_markets_dir):
    table = build_from_dataset(kalshi_trades_dir, kalshi_markets_dir, strength=10)
    assert set(table.curves[ALL]) == set(KEYS)
    assert all(len(c) == 99 for c in table.curves[ALL].values())
    assert all(0 < p < 1 for c in table.curves[ALL].values() for p in c)
    assert sum(table.counts[ALL]["yes_taker"]) > 0


# --- quotes ---------------------------------------------------------------------------


def test_quote_parses_dollar_fields_and_fills_missing_side():
    q = Quote.from_api(market(yes_bid=40, yes_ask=43))
    assert (q.yes_bid, q.yes_ask) == (40, 43)
    assert (q.no_bid, q.no_ask) == (57, 60)
    assert q.spread == 3
    assert 4.9 < q.days_to_close() < 5.1


def test_quote_treats_zero_and_hundred_as_empty_book():
    q = Quote.from_api({"ticker": "X", "yes_bid": 0, "yes_ask": 100, "no_bid": 0, "no_ask": 100})
    assert q.yes_ask is None and q.no_ask is None


# --- ideas ----------------------------------------------------------------------------


def test_kelly_fraction():
    assert kelly_fraction(0.6, 50) == pytest.approx(0.2)
    assert kelly_fraction(0.4, 50) == 0


def test_view_creates_edge_and_sizes_bet():
    q = Quote.from_api(market(yes_bid=39, yes_ask=40))
    cfg = ScanConfig(views={q.ticker: 0.55}, include_maker=False)
    ideas = evaluate_quote(q, CalibrationTable.prior(), FeeSchedule(), cfg)
    assert len(ideas) == 1
    idea = ideas[0]
    assert idea.side == "yes" and idea.source == "your view"
    assert idea.ev_cents == pytest.approx(55 - 40 - idea.fee, abs=1e-3)
    assert 0 < idea.stake <= cfg.bankroll * cfg.max_bet_fraction


def test_plan_keeps_one_bet_per_event_and_respects_bankroll():
    quotes = [
        Quote.from_api(market(f"KXTEST-{n}", yes_bid=39, yes_ask=40, event_ticker=f"EV{n // 2}")) for n in range(6)
    ]
    cfg = ScanConfig(
        bankroll=100,
        kelly_fraction=1.0,
        max_bet_fraction=0.5,
        views={q.ticker: 0.7 for q in quotes},
        include_maker=False,
    )
    ideas = rank_ideas(quotes, CalibrationTable.prior(), FeeSchedule(), cfg)
    assert len(ideas) == 3  # three events, one idea each
    plan = [i for i in ideas if i.in_plan]
    assert plan and sum(i.stake for i in plan) <= 100
    assert len(plan) < len(ideas)  # each idea stakes ~$50, so the budget binds


def test_fair_market_produces_no_ideas():
    q = Quote.from_api(market(yes_bid=49, yes_ask=51))
    assert rank_ideas([q], CalibrationTable.prior(), FeeSchedule(), ScanConfig(include_maker=False)) == []


def test_filters_drop_illiquid_markets():
    q = Quote.from_api(market(yes_bid=39, yes_ask=40, volume_24h_fp="1"))
    cfg = ScanConfig(views={}, min_volume_24h=50)
    assert rank_ideas([q], CalibrationTable.prior(), FeeSchedule(), cfg) == []


# --- arbitrage ------------------------------------------------------------------------


def test_all_no_arbitrage_in_mutually_exclusive_event():
    ev = Event.from_api(
        {
            "event_ticker": "KXRACE",
            "title": "Who wins?",
            "mutually_exclusive": True,
            "markets": [
                market("A", yes_bid=30, no_ask=62),
                market("B", yes_bid=30, no_ask=62),
                market("C", yes_bid=30, no_ask=62),
            ],
        }
    )
    arbs = find_arbitrage([ev], FeeSchedule(), contracts=100)
    no_arb = next(a for a in arbs if a.kind == "all-NO")
    assert no_arb.risk_free
    assert no_arb.min_payout_cents == 200
    assert no_arb.profit_cents == pytest.approx(200 - 3 * (62 + 1.65), abs=0.01)


def test_ladder_arbitrage():
    ev = Event.from_api(
        {
            "event_ticker": "KXHIGH",
            "title": "High temp",
            "mutually_exclusive": False,
            "markets": [
                market("KXHIGH-T80", yes_ask=40, no_ask=62, strike_type="greater", floor_strike=80),
                market("KXHIGH-T85", yes_ask=50, no_ask=45, strike_type="greater", floor_strike=85),
            ],
        }
    )
    arbs = find_arbitrage([ev], FeeSchedule())
    assert [a.kind for a in arbs] == ["ladder-above"]
    assert [(leg["ticker"], leg["side"]) for leg in arbs[0].legs] == [("KXHIGH-T80", "yes"), ("KXHIGH-T85", "no")]


def test_ladder_arbitrage_ignores_different_underlyings():
    # Two pitchers' strikeout ladders in one game event: their lines are unrelated.
    ev = Event.from_api(
        {
            "event_ticker": "KXMLBKS-GAME",
            "title": "Strikeouts",
            "mutually_exclusive": False,
            "markets": [
                market("KXMLBKS-GAME-PITCHERA-3", yes_ask=1, no_ask=99, strike_type="greater", floor_strike=2.5),
                market("KXMLBKS-GAME-PITCHERB-4", yes_ask=99, no_ask=1, strike_type="greater", floor_strike=3.5),
            ],
        }
    )
    assert find_arbitrage([ev], FeeSchedule()) == []


def test_ladder_arbitrage_ignores_other_team_in_same_event():
    # Spread markets for both teams share one event; "BSU3" and "USU2" are different ladders.
    ev = Event.from_api(
        {
            "event_ticker": "KXSPREAD-G",
            "title": "Spread",
            "mutually_exclusive": False,
            "markets": [
                market(
                    "KXSPREAD-G-USU2",
                    yes_ask=9,
                    no_ask=93,
                    strike_type="greater",
                    floor_strike=1.5,
                    custom_strike={"team": "usu"},
                ),
                market(
                    "KXSPREAD-G-BSU3",
                    yes_ask=91,
                    no_ask=11,
                    strike_type="greater",
                    floor_strike=2.5,
                    custom_strike={"team": "bsu"},
                ),
            ],
        }
    )
    assert find_arbitrage([ev], FeeSchedule()) == []


def test_arbitrage_reports_basket_capacity():
    ev = Event.from_api(
        {
            "event_ticker": "KXHIGH",
            "title": "High temp",
            "markets": [
                market("KXHIGH-T80", yes_ask=40, strike_type="greater", floor_strike=80, yes_ask_size_fp="25.00"),
                market("KXHIGH-T85", yes_bid=55, strike_type="greater", floor_strike=85, yes_bid_size_fp="7.00"),
            ],
        }
    )
    (arb,) = find_arbitrage([ev], FeeSchedule())
    assert arb.max_baskets == 7


def test_bucket_markets_are_not_ladder_rungs():
    # Kalshi labels exact-count buckets strike_type "less" with floor == cap.
    ev = Event.from_api(
        {
            "event_ticker": "KXCOUNT",
            "title": "How many?",
            "markets": [
                market("KXCOUNT-5.0", yes_ask=51, no_ask=53, strike_type="less", floor_strike=5, cap_strike=5),
                market("KXCOUNT-9.0", yes_ask=2, no_ask=99, strike_type="less", floor_strike=9, cap_strike=9),
            ],
        }
    )
    assert find_arbitrage([ev], FeeSchedule()) == []


def test_ladder_rungs_must_share_close_time():
    later = (datetime.now(timezone.utc) + timedelta(days=900)).isoformat()
    ev = Event.from_api(
        {
            "event_ticker": "USCLIMATE",
            "title": "Climate goals",
            "markets": [
                market("USCLIMATE-2025", yes_ask=14, strike_type="less_or_equal", cap_strike=4909.9),
                market("USCLIMATE-2030", yes_bid=17, strike_type="less_or_equal", cap_strike=3317.5, close_time=later),
            ],
        }
    )
    assert find_arbitrage([ev], FeeSchedule()) == []


def test_no_arbitrage_when_fairly_priced():
    ev = Event.from_api(
        {
            "event_ticker": "KXRACE",
            "title": "Who wins?",
            "mutually_exclusive": True,
            "markets": [market("A", yes_ask=51, no_ask=50), market("B", yes_ask=51, no_ask=50)],
        }
    )
    assert find_arbitrage([ev], FeeSchedule()) == []


# --- report / CLI ---------------------------------------------------------------------


def test_scan_from_file_writes_dashboard(tmp_path, capsys):
    events = [
        {
            "event_ticker": "KXRACE",
            "title": "Who wins </script>?",
            "mutually_exclusive": True,
            "markets": [
                market("A", yes_bid=30, yes_ask=33, no_ask=62),
                market("B", yes_bid=30, no_ask=62),
                market("C", yes_bid=30, no_ask=62),
            ],
        }
    ]
    src = tmp_path / "events.json"
    src.write_text(json.dumps(events))
    views = tmp_path / "views.csv"
    views.write_text("ticker,prob\nA,0.6\n")
    out = tmp_path / "out"
    edge_main(
        [
            "scan",
            "--from-file",
            str(src),
            "--views",
            str(views),
            "--out",
            str(out),
            "--calibration",
            str(CalibrationTable.prior().save(tmp_path / "cal.json")),
        ]
    )
    printed = capsys.readouterr().out
    assert "RISK-FREE" in printed
    payload = json.loads((out / "scan.json").read_text())
    assert payload["arbitrage"] and payload["ideas"][0]["ticker"] == "A"
    html = (out / "dashboard.html").read_text()
    assert html.startswith("<!doctype html>")
    assert "window.SCAN_DATA = {" in html
    assert "</script>?" not in html  # titles are escaped inside the data script


def test_build_payload_has_curves():
    cfg = ScanConfig()
    payload = build_payload([], [], CalibrationTable.prior(), cfg, 0)
    assert set(payload["calibration"]["ALL"]) == set(KEYS)
    assert write_outputs  # imported for the CLI path above


def test_eval_command(capsys):
    edge_main(["eval", "--side", "no", "--price", "94", "--prob", "97"])
    out = capsys.readouterr().out
    assert "EV per contract: +" in out
