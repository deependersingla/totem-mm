"""Replay test suite — auto-discovery + YAML-driven runner.

Drop a `match_capture_*.db` into `captures/`, it shows up in the suite. An
optional `replay_tests.yaml` whitelists/orders specific captures and lets
you set per-test overrides. No code change needed to add or remove tests.

Suite YAML schema (all fields optional):

    defaults:
      starting_cash_usdc: 10000
      category: SPORTS

    tests:
      - slug: cricipl-roy-del-2026-04-18      # required
        starting_cash_usdc: 5000              # override per test (optional)
        start_ts_ms: ...                      # truncate front of replay (optional)
        end_ts_ms: ...                        # truncate back (optional)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

try:
    import yaml as _yaml
except ImportError:  # pragma: no cover
    _yaml = None

from .engine import Engine
from .enums import MarketCategory
from .metrics import MetricsReport
from .replay import load_market, stream_events
from .strategy import Strategy


CAPTURE_GLOB = "match_capture_*.db"
SLUG_RE = re.compile(r"match_capture_(?P<slug>.+?)_\d{8}\.db$")


@dataclass
class TestSpec:
    slug: str
    db_path: Path
    starting_cash_usdc: float = 0.0          # informational only
    max_position_shares: Optional[float] = None  # None = no cap
    category: MarketCategory = MarketCategory.SPORTS
    start_ts_ms: Optional[int] = None
    end_ts_ms: Optional[int] = None
    include_cricket: bool = False
    cricket_lead_ms: int = 0
    cricket_lead_ms_by_signal: Optional[dict[str, int]] = None


@dataclass
class TestResult:
    spec: TestSpec
    report: MetricsReport
    error: Optional[str] = None


@dataclass
class SuiteConfig:
    captures_dir: Path
    starting_cash_usdc: float = 0.0          # informational only
    max_position_shares: Optional[float] = None
    category: MarketCategory = MarketCategory.SPORTS
    yaml_path: Optional[Path] = None
    include_slugs: Optional[set[str]] = None    # CLI --include
    exclude_slugs: set[str] = field(default_factory=set)
    include_cricket: bool = False
    cricket_lead_ms: int = 0
    cricket_lead_ms_by_signal: Optional[dict[str, int]] = None


# ── Discovery ────────────────────────────────────────────────────────


def discover_captures(captures_dir: Path) -> list[tuple[str, Path]]:
    """Return [(slug, db_path), ...] sorted by slug for every capture file."""
    if not captures_dir.exists():
        return []
    out: list[tuple[str, Path]] = []
    for p in sorted(captures_dir.glob(CAPTURE_GLOB)):
        m = SLUG_RE.match(p.name)
        if not m:
            continue
        out.append((m.group("slug"), p))
    return out


def build_specs(cfg: SuiteConfig) -> list[TestSpec]:
    discovered = dict(discover_captures(cfg.captures_dir))
    if not discovered:
        raise FileNotFoundError(
            f"no captures found in {cfg.captures_dir}/{CAPTURE_GLOB}"
        )

    yaml_overrides: dict[str, dict] = {}
    yaml_order: Optional[list[str]] = None
    yaml_defaults: dict = {}
    if cfg.yaml_path and cfg.yaml_path.exists():
        if _yaml is None:
            raise RuntimeError(
                "replay_tests.yaml supplied but PyYAML is not installed; "
                "`pip install pyyaml` or remove --yaml"
            )
        data = _yaml.safe_load(cfg.yaml_path.read_text()) or {}
        yaml_defaults = data.get("defaults") or {}
        tests = data.get("tests") or []
        yaml_order = [t["slug"] for t in tests if "slug" in t]
        yaml_overrides = {t["slug"]: t for t in tests if "slug" in t}

    starting_cash = float(yaml_defaults.get("starting_cash_usdc", cfg.starting_cash_usdc))
    max_pos_default = yaml_defaults.get("max_position_shares", cfg.max_position_shares)
    category_str = yaml_defaults.get("category", cfg.category.value)
    category = MarketCategory(category_str)

    if yaml_order is not None:
        slugs = [s for s in yaml_order if s in discovered]
    else:
        slugs = sorted(discovered.keys())

    if cfg.include_slugs is not None:
        slugs = [s for s in slugs if s in cfg.include_slugs]
    if cfg.exclude_slugs:
        slugs = [s for s in slugs if s not in cfg.exclude_slugs]

    specs: list[TestSpec] = []
    for slug in slugs:
        ov = yaml_overrides.get(slug, {})
        specs.append(TestSpec(
            slug=slug,
            db_path=discovered[slug],
            starting_cash_usdc=float(ov.get("starting_cash_usdc", starting_cash)),
            max_position_shares=ov.get("max_position_shares", max_pos_default),
            category=MarketCategory(ov.get("category", category.value)),
            start_ts_ms=ov.get("start_ts_ms"),
            end_ts_ms=ov.get("end_ts_ms"),
            include_cricket=cfg.include_cricket,
            cricket_lead_ms=cfg.cricket_lead_ms,
            cricket_lead_ms_by_signal=cfg.cricket_lead_ms_by_signal,
        ))
    return specs


# ── Run ──────────────────────────────────────────────────────────────


def run_one(spec: TestSpec, strategy_factory) -> TestResult:
    """Run a single replay. `strategy_factory` is a no-arg callable returning a
    fresh Strategy instance — important so each test starts from a clean state.
    """
    try:
        market = load_market(spec.db_path, category=spec.category)
        engine = Engine(
            market,
            starting_cash_usdc=spec.starting_cash_usdc,
            max_position_shares=spec.max_position_shares,
        )
        strat = strategy_factory()
        engine.register(strat)
        engine.run(stream_events(
            spec.db_path,
            start_ts_ms=spec.start_ts_ms, end_ts_ms=spec.end_ts_ms,
            include_cricket=spec.include_cricket,
            cricket_lead_ms=spec.cricket_lead_ms,
            cricket_lead_ms_by_signal=spec.cricket_lead_ms_by_signal,
        ))
        return TestResult(spec=spec, report=engine.metrics.report())
    except Exception as ex:
        return TestResult(
            spec=spec,
            report=MetricsReport(
                total_pnl_usdc=0, realized_pnl_usdc=0, unrealized_pnl_usdc=0,
                fees_paid_usdc=0, num_fills=0, num_maker_fills=0, num_taker_fills=0,
                maker_share_pct=0.0, volume_shares=0, volume_notional_usdc=0,
                sharpe_per_sample=None, max_drawdown_usdc=0,
                adverse_selection_bps_avg=None,
            ),
            error=f"{type(ex).__name__}: {ex}",
        )


def run_suite(cfg: SuiteConfig, strategy_factory) -> list[TestResult]:
    return [run_one(spec, strategy_factory) for spec in build_specs(cfg)]


def format_summary(results: Iterable[TestResult]) -> str:
    lines = []
    lines.append(f"{'match':<40} {'pnl':>9} {'fills':>5} {'maker%':>6} "
                 f"{'adv_bps':>9} {'volume':>8}")
    lines.append("-" * 90)
    n = 0
    pnl_total = 0.0
    fills_total = 0
    vol_total = 0.0
    winners = 0
    for r in results:
        slug = r.spec.slug[:40]
        if r.error:
            lines.append(f"{slug:<40}  ERROR: {r.error}")
            continue
        rep = r.report
        adv = "n/a" if rep.adverse_selection_bps_avg is None else f"{rep.adverse_selection_bps_avg:+.1f}"
        lines.append(
            f"{slug:<40} {rep.total_pnl_usdc:>+9.2f} {rep.num_fills:>5} "
            f"{rep.maker_share_pct:>5.1f}% {adv:>9} {rep.volume_shares:>8.0f}"
        )
        n += 1
        pnl_total += rep.total_pnl_usdc
        fills_total += rep.num_fills
        vol_total += rep.volume_shares
        if rep.total_pnl_usdc > 0:
            winners += 1
    lines.append("-" * 90)
    if n:
        lines.append(
            f"{'AGGREGATE':<40} {pnl_total:>+9.2f} {fills_total:>5} "
            f"{'':>6} {'':>9} {vol_total:>8.0f}"
        )
        lines.append(f"winners: {winners}/{n}  mean pnl: {pnl_total/n:+.2f}")
    return "\n".join(lines)


def write_run_outputs(out_dir: Path, results: Iterable[TestResult]) -> None:
    """Per-test artifacts: trade_log.csv (recorded fills), summary.json."""
    out_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, dict] = {}
    for r in results:
        if r.error:
            summary[r.spec.slug] = {"error": r.error}
            continue
        rep = r.report.as_dict()
        summary[r.spec.slug] = rep
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
