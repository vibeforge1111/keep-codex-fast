"""Tests for CLI flag definitions in keep_codex_fast.py."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

# Ensure the scripts directory is importable
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture
def parser() -> argparse.ArgumentParser:
    """Return the argument parser from keep_codex_fast."""
    from scripts import keep_codex_fast as kcf  # type: ignore[import]

    # Reuse the internal parser factory via parse_args signature
    return kcf.parse_args.__wrapped__ if hasattr(kcf.parse_args, "__wrapped__") else kcf.parse_args([])


def test_apply_flag() -> None:
    from scripts.keep_codex_fast import parse_args
    args = parse_args(["--apply"])
    assert args.apply is True


def test_backup_only_flag() -> None:
    from scripts.keep_codex_fast import parse_args
    args = parse_args(["--backup-only"])
    assert args.backup_only is True


def test_details_flag() -> None:
    from scripts.keep_codex_fast import parse_args
    args = parse_args(["--details"])
    assert args.details is True


def test_wait_for_codex_exit_flag() -> None:
    from scripts.keep_codex_fast import parse_args
    args = parse_args(["--wait-for-codex-exit"])
    assert args.wait_for_codex_exit is True


def test_codex_home_default() -> None:
    from scripts.keep_codex_fast import parse_args
    args = parse_args([])
    assert args.codex_home is None


def test_backup_root_default() -> None:
    from scripts.keep_codex_fast import parse_args
    args = parse_args([])
    assert args.backup_root is None


def test_archive_older_than_days_default() -> None:
    from scripts.keep_codex_fast import parse_args
    args = parse_args([])
    assert args.archive_older_than_days == 10


def test_worktree_older_than_days_default() -> None:
    from scripts.keep_codex_fast import parse_args
    args = parse_args([])
    assert args.worktree_older_than_days == 7


def test_rotate_logs_above_mb_default() -> None:
    from scripts.keep_codex_fast import parse_args
    args = parse_args([])
    assert args.rotate_logs_above_mb == 64


def test_thread_title_limit_default() -> None:
    from scripts.keep_codex_fast import parse_args
    args = parse_args([])
    assert args.thread_title_limit == 120


def test_thread_preview_limit_default() -> None:
    from scripts.keep_codex_fast import parse_args
    args = parse_args([])
    assert args.thread_preview_limit == 240


def test_repair_thread_metadata_bloat_flag() -> None:
    from scripts.keep_codex_fast import parse_args
    args = parse_args(["--repair-thread-metadata-bloat"])
    assert args.repair_thread_metadata_bloat is True


def test_apply_and_backup_only_mutual_exclusion() -> None:
    from scripts.keep_codex_fast import parse_args
    with pytest.raises(SystemExit):
        parse_args(["--apply", "--backup-only"])


def test_title_limit_minimum() -> None:
    from scripts.keep_codex_fast import parse_args
    with pytest.raises(SystemExit):
        parse_args(["--thread-title-limit", "10"])
