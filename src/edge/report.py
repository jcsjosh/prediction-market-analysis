"""Write scan results as JSON, CSV and a self-contained HTML dashboard."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from src.edge.calibration import CalibrationTable
from src.edge.strategy import Arb, Idea, ScanConfig

TEMPLATE = Path(__file__).with_name("dashboard.html")
DATA_MARKER = "window.SCAN_DATA = null;"
# The template is a page body; give the local copy a real document shell.
PAGE_HEAD = (
    '<!doctype html><html><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    "<style>[hidden]{display:none!important}</style></head><body>"
)


def build_payload(ideas: list[Idea], arbs: list[Arb], cal: CalibrationTable, cfg: ScanConfig, n_markets: int) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "calibration_source": cal.source,
        "markets_scanned": n_markets,
        "config": {k: v for k, v in vars(cfg).items() if k != "views"},
        "views": len(cfg.views),
        "ideas": [i.to_dict() for i in ideas],
        "arbitrage": [a.to_dict() for a in arbs],
        "calibration": {"ALL": cal.curves["ALL"]},
    }


def write_outputs(payload: dict, out_dir: Path | str) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {"json": out / "scan.json", "csv": out / "ideas.csv", "html": out / "dashboard.html"}

    paths["json"].write_text(json.dumps(payload, indent=2, default=str))

    ideas = payload["ideas"]
    with paths["csv"].open("w", newline="") as f:
        if ideas:
            writer = csv.DictWriter(f, fieldnames=list(ideas[0].keys()))
            writer.writeheader()
            writer.writerows(ideas)

    html = TEMPLATE.read_text()
    # Escape "</" so market titles can never close the script tag early.
    data = json.dumps(payload, default=str).replace("</", "<\\/")
    page = html.replace(DATA_MARKER, f"window.SCAN_DATA = {data};", 1)
    paths["html"].write_text(PAGE_HEAD + page + "</body></html>\n")
    return paths


def format_table(ideas: list[Idea], top: int) -> str:
    if not ideas:
        return "No bets clear the edge threshold right now. That is a valid answer - most markets are fairly priced."
    header = f"{'#':>2}  {'Action':<28} {'Win%':>6} {'EV¢':>6} {'ROI':>6} {'Days':>6} {'Qty':>5} {'Stake$':>8} {'E[P]$':>7}  Market"
    lines = [header, "-" * len(header)]
    for n, i in enumerate(ideas[:top], 1):
        days = f"{i.days_to_close:.1f}" if i.days_to_close is not None else "?"
        name = f"{i.title} — {i.subtitle}" if i.subtitle and i.subtitle not in i.title else i.title
        star = "*" if i.source != "calibration" else " "
        lines.append(
            f"{n:>2}{star} {i.action:<28} {i.win_prob * 100:>5.1f}% {i.ev_cents:>6.2f} {i.roi * 100:>5.1f}% "
            f"{days:>6} {i.contracts:>5} {i.stake:>8.2f} {i.expected_profit:>7.2f}  {name[:70]} [{i.ticker}]"
        )
    return "\n".join(lines)


def format_arbs(arbs: list[Arb], top: int) -> str:
    if not arbs:
        return "No arbitrage baskets found."
    lines = []
    for a in arbs[:top]:
        tag = "RISK-FREE" if a.risk_free else "CHECK RULES"
        legs = ", ".join(f"{leg['side'].upper()} {leg['ticker']} @ {leg['price']:g}¢" for leg in a.legs[:6])
        more = f" (+{len(a.legs) - 6} more)" if len(a.legs) > 6 else ""
        lines.append(
            f"[{tag}] {a.kind}: {a.title} — cost {a.cost_cents:.1f}¢, pays ≥ {a.min_payout_cents:.0f}¢, "
            f"profit {a.profit_cents:.2f}¢/basket ({a.roi * 100:.2f}%)\n    {legs}{more}\n    {a.note}"
        )
    return "\n".join(lines)
