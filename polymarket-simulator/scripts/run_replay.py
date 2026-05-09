"""Run a strategy against a single capture and print metrics + write outputs.

    python scripts/run_replay.py \
        --capture captures/match_capture_cricipl-roy-del-2026-04-18_*.db \
        --strategy strategies.inventory_balancing_mm:InventoryBalancingMM
"""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path

from backtest.engine import Engine
from backtest.metrics import MetricsReport
from backtest.replay import load_market, stream_events


def _import_strategy(spec: str):
    module_name, _, class_name = spec.partition(":")
    if not class_name:
        raise SystemExit(f"--strategy must be 'module:Class', got {spec!r}")
    return getattr(importlib.import_module(module_name), class_name)


def _print_report(slug: str, report: MetricsReport) -> None:
    print(f"\n=== {slug} ===")
    print(f"  total_pnl_usdc:           {report.total_pnl_usdc:+.4f}")
    print(f"  realized / unrealized:    {report.realized_pnl_usdc:+.4f} / {report.unrealized_pnl_usdc:+.4f}")
    print(f"  fees_paid_usdc:           {report.fees_paid_usdc:.4f}")
    print(f"  fills:                    {report.num_fills}  "
          f"(maker {report.num_maker_fills}, taker {report.num_taker_fills}, "
          f"maker_share {report.maker_share_pct:.1f}%)")
    print(f"  volume:                   {report.volume_shares:.2f} shares  "
          f"${report.volume_notional_usdc:.2f} notional")
    print(f"  sharpe (per-sample):      {report.sharpe_per_sample}")
    print(f"  max drawdown:             {report.max_drawdown_usdc:.4f}")
    print(f"  adverse_selection_bps:    {report.adverse_selection_bps_avg}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", required=True, type=Path)
    ap.add_argument("--strategy", required=True, help="module:Class")
    ap.add_argument("--starting-cash-usdc", type=float, default=0.0,
                    help="informational only; the engine never enforces a cash cap")
    ap.add_argument("--max-position-shares", type=float, default=None,
                    help="optional hard cap on absolute position per token")
    ap.add_argument("--include-cricket", action="store_true",
                    help="emit CricketEvent in the stream (default off)")
    ap.add_argument("--cricket-lead-ms", type=int, default=0,
                    help="subtract this from cricket event ts to simulate a faster feed")
    ap.add_argument("--start-ts-ms", type=int, default=None)
    ap.add_argument("--end-ts-ms", type=int, default=None)
    ap.add_argument("--out-summary", type=Path, default=None,
                    help="optional JSON summary output")
    args = ap.parse_args()

    if not args.capture.exists():
        raise SystemExit(f"capture not found: {args.capture}")

    StrategyCls = _import_strategy(args.strategy)
    market = load_market(args.capture)
    engine = Engine(
        market,
        starting_cash_usdc=args.starting_cash_usdc,
        max_position_shares=args.max_position_shares,
    )
    engine.register(StrategyCls())
    engine.run(stream_events(
        args.capture,
        start_ts_ms=args.start_ts_ms, end_ts_ms=args.end_ts_ms,
        include_cricket=args.include_cricket,
        cricket_lead_ms=args.cricket_lead_ms,
    ))

    report = engine.metrics.report()
    _print_report(market.slug, report)

    if args.out_summary:
        args.out_summary.write_text(json.dumps(report.as_dict(), indent=2, default=str))
        print(f"\nsummary written to {args.out_summary}")


if __name__ == "__main__":
    main()
