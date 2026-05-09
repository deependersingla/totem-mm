"""Run a strategy across all replay tests and print a summary table.

    python scripts/run_suite.py --strategy strategies.inventory_balancing_mm:InventoryBalancingMM

Optional:
    --captures-dir <dir>    default: ../captures
    --yaml <file>           default: replay_tests.yaml in cwd if it exists
    --include slug ...      only run these slugs
    --exclude slug ...      skip these slugs
    --out-dir <dir>         per-test summary.json output
"""

from __future__ import annotations

import argparse
import importlib
from pathlib import Path

from backtest.suite import (
    SuiteConfig, format_summary, run_suite, write_run_outputs,
)


def _import_strategy_factory(spec: str):
    module_name, _, class_name = spec.partition(":")
    if not class_name:
        raise SystemExit(f"--strategy must be 'module:Class', got {spec!r}")
    cls = getattr(importlib.import_module(module_name), class_name)
    return cls   # callable, no-arg → fresh strategy per test


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", required=True, help="module:Class")
    ap.add_argument("--captures-dir", type=Path,
                    default=Path(__file__).resolve().parent.parent.parent / "captures")
    ap.add_argument("--yaml", type=Path, default=None,
                    help="path to replay_tests.yaml (default: ./replay_tests.yaml if exists)")
    ap.add_argument("--starting-cash-usdc", type=float, default=0.0,
                    help="informational only; engine does not enforce a cash cap")
    ap.add_argument("--max-position-shares", type=float, default=None,
                    help="optional hard cap on absolute position per token")
    ap.add_argument("--include-cricket", action="store_true",
                    help="emit CricketEvent in the stream (default off)")
    ap.add_argument("--cricket-lead-ms", type=int, default=0,
                    help="subtract this from cricket event ts to simulate a faster feed")
    ap.add_argument("--cricket-lead-boundary-ms", type=int, default=None,
                    help="per-signal lead override for 4/6 (else uses --cricket-lead-ms)")
    ap.add_argument("--cricket-lead-wicket-ms", type=int, default=None,
                    help="per-signal lead override for W (else uses --cricket-lead-ms)")
    ap.add_argument("--include", nargs="*", default=None)
    ap.add_argument("--exclude", nargs="*", default=[])
    ap.add_argument("--out-dir", type=Path, default=None)
    args = ap.parse_args()

    yaml_path = args.yaml
    if yaml_path is None:
        candidate = Path("replay_tests.yaml")
        if candidate.exists():
            yaml_path = candidate

    by_signal = None
    if args.cricket_lead_boundary_ms is not None or args.cricket_lead_wicket_ms is not None:
        by_signal = {}
        if args.cricket_lead_boundary_ms is not None:
            by_signal["4"] = args.cricket_lead_boundary_ms
            by_signal["6"] = args.cricket_lead_boundary_ms
        if args.cricket_lead_wicket_ms is not None:
            by_signal["W"] = args.cricket_lead_wicket_ms

    cfg = SuiteConfig(
        captures_dir=args.captures_dir,
        starting_cash_usdc=args.starting_cash_usdc,
        max_position_shares=args.max_position_shares,
        yaml_path=yaml_path,
        include_slugs=set(args.include) if args.include else None,
        exclude_slugs=set(args.exclude),
        include_cricket=args.include_cricket,
        cricket_lead_ms=args.cricket_lead_ms,
        cricket_lead_ms_by_signal=by_signal,
    )

    factory = _import_strategy_factory(args.strategy)
    results = run_suite(cfg, factory)

    print(format_summary(results))

    if args.out_dir:
        write_run_outputs(args.out_dir, results)
        print(f"\nper-test summaries written under {args.out_dir}")


if __name__ == "__main__":
    main()
