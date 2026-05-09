"""Suite discovery + YAML config tests (no engine runs)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from backtest.suite import (
    SuiteConfig, build_specs, discover_captures, format_summary,
)


def _make_capture(path: Path, slug: str) -> Path:
    """Create a minimal capture DB with required tables/match_meta only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript("""
            CREATE TABLE match_meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE book_snapshots (id INTEGER PRIMARY KEY, local_ts_ms INTEGER, asset_id TEXT);
            CREATE TABLE trades (id INTEGER PRIMARY KEY, clob_ts_ms INTEGER, asset_id TEXT);
            CREATE TABLE cricket_events (id INTEGER PRIMARY KEY, local_ts_ms INTEGER, signal_type TEXT);
        """)
        conn.executemany(
            "INSERT INTO match_meta(key, value) VALUES (?, ?)",
            [
                ("slug", slug), ("condition_id", "c"),
                ("token_ids", '["A","B"]'),
                ("outcome_names", '["Yes","No"]'),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return path


def test_discover_finds_captures(tmp_path: Path):
    _make_capture(tmp_path / "match_capture_cricipl-aaa-2026-04-01_20260401.db", "cricipl-aaa-2026-04-01")
    _make_capture(tmp_path / "match_capture_cricipl-bbb-2026-04-02_20260402.db", "cricipl-bbb-2026-04-02")
    found = discover_captures(tmp_path)
    slugs = [s for s, _ in found]
    assert slugs == ["cricipl-aaa-2026-04-01", "cricipl-bbb-2026-04-02"]


def test_discover_empty_dir(tmp_path: Path):
    assert discover_captures(tmp_path) == []


def test_build_specs_default_runs_all(tmp_path: Path):
    _make_capture(tmp_path / "match_capture_cricipl-aaa-2026-04-01_20260401.db", "cricipl-aaa-2026-04-01")
    _make_capture(tmp_path / "match_capture_cricipl-bbb-2026-04-02_20260402.db", "cricipl-bbb-2026-04-02")
    specs = build_specs(SuiteConfig(captures_dir=tmp_path, starting_cash_usdc=5000))
    assert [s.slug for s in specs] == ["cricipl-aaa-2026-04-01", "cricipl-bbb-2026-04-02"]
    assert all(s.starting_cash_usdc == 5000 for s in specs)


def test_build_specs_yaml_whitelist(tmp_path: Path):
    pytest.importorskip("yaml")
    _make_capture(tmp_path / "match_capture_cricipl-aaa-2026-04-01_20260401.db", "cricipl-aaa-2026-04-01")
    _make_capture(tmp_path / "match_capture_cricipl-bbb-2026-04-02_20260402.db", "cricipl-bbb-2026-04-02")
    yaml_path = tmp_path / "tests.yaml"
    yaml_path.write_text(
        "defaults:\n  starting_cash_usdc: 99\n"
        "tests:\n  - slug: cricipl-bbb-2026-04-02\n    starting_cash_usdc: 42\n"
    )
    specs = build_specs(SuiteConfig(captures_dir=tmp_path, yaml_path=yaml_path))
    assert [s.slug for s in specs] == ["cricipl-bbb-2026-04-02"]
    assert specs[0].starting_cash_usdc == 42


def test_build_specs_include_filter(tmp_path: Path):
    _make_capture(tmp_path / "match_capture_cricipl-aaa-2026-04-01_20260401.db", "cricipl-aaa-2026-04-01")
    _make_capture(tmp_path / "match_capture_cricipl-bbb-2026-04-02_20260402.db", "cricipl-bbb-2026-04-02")
    specs = build_specs(SuiteConfig(
        captures_dir=tmp_path,
        include_slugs={"cricipl-aaa-2026-04-01"},
    ))
    assert [s.slug for s in specs] == ["cricipl-aaa-2026-04-01"]


def test_build_specs_no_captures_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        build_specs(SuiteConfig(captures_dir=tmp_path))


def test_format_summary_empty():
    s = format_summary([])
    assert "match" in s
    assert "AGGREGATE" not in s
