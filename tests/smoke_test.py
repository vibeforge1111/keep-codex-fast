#!/usr/bin/env python3
"""Smoke tests for keep-codex-fast using a fake Codex home."""

from __future__ import annotations

import argparse
import contextlib
import io
import importlib.util
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "keep_codex_fast.py"


def load_module():
    spec = importlib.util.spec_from_file_location("keep_codex_fast", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["keep_codex_fast"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)

    module._real_top_node_processes = module.top_node_processes
    module.codex_processes_running = lambda: []

    def top_node_processes_stub(details=False):
        module.report("top_node_processes skipped_in_smoke")
        return True

    module.top_node_processes = top_node_processes_stub
    return module


def assert_top_node_processes_helper(module) -> None:
    helper = module._real_top_node_processes
    captured_commands = []

    def empty_windows_result(command, **kwargs):
        captured_commands.append(command)
        assert kwargs.get("text") is True
        return "[]"

    output = io.StringIO()
    with mock.patch.object(module.platform, "system", return_value="Windows"):
        with mock.patch.object(
            module.subprocess,
            "check_output",
            side_effect=empty_windows_result,
        ):
            with contextlib.redirect_stdout(output):
                assert helper(False) is True

    command_text = captured_commands[0][-1]
    assert "$processes = @(" in command_text
    assert "if ($processes.Count -eq 0)" in command_text
    assert "Write-Output '[]'" in command_text
    assert "exit 0" in command_text

    output = io.StringIO()
    with mock.patch.object(module.platform, "system", return_value="Windows"):
        with mock.patch.object(
            module.subprocess,
            "check_output",
            return_value="",
        ):
            with contextlib.redirect_stdout(output):
                assert helper(False) is False

    text = output.getvalue()
    assert "node_process_report_skipped empty PowerShell process output" in text

    output = io.StringIO()
    with mock.patch.object(module.platform, "system", return_value="Windows"):
        with mock.patch.object(
            module.subprocess,
            "check_output",
            return_value="null",
        ):
            with contextlib.redirect_stdout(output):
                assert helper(False) is False

    text = output.getvalue()
    assert "node_process_report_skipped unexpected PowerShell JSON null" in text

    output = io.StringIO()
    with mock.patch.object(module.platform, "system", return_value="Windows"):
        with mock.patch.object(
            module.subprocess,
            "check_output",
            return_value="{not-json",
        ):
            with contextlib.redirect_stdout(output):
                assert helper(False) is False

    text = output.getvalue()
    assert "node_process_report_skipped malformed PowerShell JSON" in text

    windows_payloads = [
        (
            '{"Id":123,"ProcessName":"node","MB":12.5,'
            '"Path":"C:\\\\secret\\\\node.exe"}'
        ),
        (
            '[{"Id":123,"ProcessName":"node","MB":12.5,'
            '"Path":"C:\\\\secret\\\\node.exe"}]'
        ),
    ]
    for payload in windows_payloads:
        output = io.StringIO()
        with mock.patch.object(module.platform, "system", return_value="Windows"):
            with mock.patch.object(
                module.subprocess,
                "check_output",
                return_value=payload,
            ):
                with contextlib.redirect_stdout(output):
                    assert helper(False) is True

        text = output.getvalue()
        assert "node_mb 12.5 process=node" in text
        assert "pid=123" not in text
        assert r"C:\secret\node.exe" not in text

    output = io.StringIO()
    with mock.patch.object(module.platform, "system", return_value="Windows"):
        with mock.patch.object(
            module.subprocess,
            "check_output",
            return_value="[null]",
        ):
            with contextlib.redirect_stdout(output):
                assert helper(False) is False

    text = output.getvalue()
    assert "node_process_report_skipped unexpected PowerShell process row" in text

    output = io.StringIO()
    with mock.patch.object(module.platform, "system", return_value="Windows"):
        with mock.patch.object(
            module.subprocess,
            "check_output",
            side_effect=OSError("process query unavailable"),
        ):
            with contextlib.redirect_stdout(output):
                assert helper(False) is False

    text = output.getvalue()
    assert "node_process_report_skipped process query unavailable" in text

    output = io.StringIO()
    with mock.patch.object(module.platform, "system", return_value="Linux"):
        with mock.patch.object(
            module.subprocess,
            "check_output",
            return_value="",
        ):
            with contextlib.redirect_stdout(output):
                assert helper(False) is True

    output = io.StringIO()
    with mock.patch.object(module.platform, "system", return_value="Linux"):
        with mock.patch.object(
            module.subprocess,
            "check_output",
            return_value="123 2048 node node server.js\n",
        ):
            with contextlib.redirect_stdout(output):
                assert helper(False) is True

    text = output.getvalue()
    assert "node_mb 2.0 process=node" in text


def make_fake_home(root: Path) -> dict[str, Path]:
    codex_home = root / ".codex"
    sessions = codex_home / "sessions" / "2026" / "01" / "01"
    sessions.mkdir(parents=True)
    rollout = sessions / "rollout-2026-01-01T00-00-00-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.jsonl"
    rollout.write_text('{"type":"test"}\n', encoding="utf-8")
    old_time = time.time() - 30 * 86400
    os.utime(rollout, (old_time, old_time))

    (codex_home / ".codex-global-state.json").write_text('{"pinned-thread-ids":[]}', encoding="utf-8")
    (codex_home / "config.toml").write_text(
        '[projects."C:\\\\DefinitelyMissingKeepCodexFast"]\ntrust_level = "trusted"\n',
        encoding="utf-8",
    )

    worktree = codex_home / "worktrees" / "oldtree"
    worktree.mkdir(parents=True)
    (worktree / "file.txt").write_text("x", encoding="utf-8")
    os.utime(worktree, (old_time, old_time))

    log_file = codex_home / "logs_2.sqlite"
    log_file.write_text("log", encoding="utf-8")

    state_db = codex_home / "state_5.sqlite"
    conn = sqlite3.connect(state_db)
    conn.execute(
        "create table threads (id text primary key, title text, first_user_message text, rollout_path text, cwd text, updated_at integer, archived_at integer, archived integer)"
    )
    long_title = "Title " + ("x" * 300)
    long_preview = "Preview " + ("y" * 600)
    conn.execute(
        "insert into threads values (?,?,?,?,?,?,?,?)",
        (
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            long_title,
            long_preview,
            str(rollout),
            r"\\?\C:\DefinitelyMissingKeepCodexFast",
            int(old_time),
            None,
            0,
        ),
    )
    conn.commit()
    conn.close()

    return {
        "codex_home": codex_home,
        "rollout": rollout,
        "worktree": worktree,
        "log_file": log_file,
        "state_db": state_db,
    }


def assert_report_mode(module) -> None:
    with tempfile.TemporaryDirectory() as td:
        paths = make_fake_home(Path(td))
        backup = Path(td) / "backup-report"
        args = argparse.Namespace(
            apply=False,
            backup_only=False,
            details=False,
            wait_for_codex_exit=False,
            codex_home=str(paths["codex_home"]),
            backup_root=str(backup),
            archive_older_than_days=10,
            worktree_older_than_days=7,
            rotate_logs_above_mb=0,
            thread_title_limit=120,
            thread_preview_limit=240,
            repair_thread_metadata_bloat=False,
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            assert module.run(args) == 0
        text = output.getvalue()
        terminal_lines = [
            "done",
            "result_schema_version 1",
            "overall_status_scope KEEP_CODEX_FAST_REPORT_PILOT",
            "required_check top_node_processes PASS",
            "overall_status COMPLETE",
            "overall_status_zh 已完成",
            "next_action NONE",
        ]
        assert text.rstrip().splitlines()[-len(terminal_lines):] == terminal_lines
        assert "required_check top_node_processes FAIL" not in text
        assert "overall_status PARTIAL" not in text
        assert "issue top_node_processes REQUIRED_CHECK_FAILED" not in text
        assert paths["rollout"].exists(), "report mode must not move sessions"
        assert paths["worktree"].exists(), "report mode must not move worktrees"
        assert paths["log_file"].exists(), "report mode must not rotate logs"
        assert not backup.exists(), "report mode must not create backup artifacts"
        assert "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" not in text
        assert str(paths["codex_home"]) not in text
        conn = sqlite3.connect(paths["state_db"])
        title, preview = conn.execute(
            "select title, first_user_message from threads where id=?",
            ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",),
        ).fetchone()
        conn.close()
        assert len(title) > 120, "report mode must not trim titles"
        assert len(preview) > 240, "report mode must not trim previews"


def assert_partial_required_check(module) -> None:
    with tempfile.TemporaryDirectory() as td:
        paths = make_fake_home(Path(td))
        args = argparse.Namespace(
            apply=False,
            backup_only=False,
            details=False,
            wait_for_codex_exit=False,
            codex_home=str(paths["codex_home"]),
            backup_root=str(Path(td) / "backup-partial"),
            archive_older_than_days=10,
            worktree_older_than_days=7,
            rotate_logs_above_mb=0,
            thread_title_limit=120,
            thread_preview_limit=240,
            repair_thread_metadata_bloat=False,
        )

        original = module.top_node_processes
        output = io.StringIO()
        try:
            module.top_node_processes = lambda details=False: False
            with contextlib.redirect_stdout(output):
                exit_code = module.run(args)
        finally:
            module.top_node_processes = original

        text = output.getvalue()
        terminal_lines = [
            "done",
            "result_schema_version 1",
            "overall_status_scope KEEP_CODEX_FAST_REPORT_PILOT",
            "required_check top_node_processes FAIL",
            "issue top_node_processes REQUIRED_CHECK_FAILED",
            "issue_zh top_node_processes 必需检查失败；核心维护报告仍可使用",
            "overall_status PARTIAL",
            "overall_status_zh 部分完成",
            "next_action RETRY_REQUIRED_STEP",
        ]

        assert exit_code == 0
        assert text.rstrip().splitlines()[-len(terminal_lines):] == terminal_lines
        assert "required_check top_node_processes PASS" not in text
        assert "overall_status COMPLETE" not in text
        assert "next_action NONE" not in text


def assert_missing_codex_home_preserves_early_exit(module) -> None:
    with tempfile.TemporaryDirectory() as td:
        missing_home = Path(td) / "missing-codex-home"
        args = argparse.Namespace(
            apply=False,
            backup_only=False,
            details=False,
            wait_for_codex_exit=False,
            codex_home=str(missing_home),
            backup_root=str(Path(td) / "unused-backup"),
            archive_older_than_days=10,
            worktree_older_than_days=7,
            rotate_logs_above_mb=0,
            thread_title_limit=120,
            thread_preview_limit=240,
            repair_thread_metadata_bloat=False,
        )

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = module.run(args)

        text = output.getvalue()
        lines = text.rstrip().splitlines()

        assert exit_code == 2
        assert lines == [f"codex_home_missing {missing_home.resolve()}"]
        assert "done" not in lines
        assert "result_schema_version 1" not in text
        assert "overall_status_scope KEEP_CODEX_FAST_REPORT_PILOT" not in text
        assert "required_check top_node_processes" not in text
        assert "overall_status COMPLETE" not in text
        assert "overall_status PARTIAL" not in text
        assert "next_action NONE" not in text
        assert "next_action RETRY_REQUIRED_STEP" not in text

def assert_backup_only_mode(module) -> None:
    with tempfile.TemporaryDirectory() as td:
        paths = make_fake_home(Path(td))
        backup = Path(td) / "backup-only"
        args = argparse.Namespace(
            apply=False,
            backup_only=True,
            details=False,
            wait_for_codex_exit=False,
            codex_home=str(paths["codex_home"]),
            backup_root=str(backup),
            archive_older_than_days=10,
            worktree_older_than_days=7,
            rotate_logs_above_mb=0,
            thread_title_limit=120,
            thread_preview_limit=240,
            repair_thread_metadata_bloat=False,
        )
        assert module.run(args) == 0
        assert paths["rollout"].exists(), "backup-only mode must not move sessions"
        assert paths["worktree"].exists(), "backup-only mode must not move worktrees"
        assert paths["log_file"].exists(), "backup-only mode must not rotate logs"
        assert (backup / "state_5.sqlite").exists()
        assert (backup / "config.toml").exists()
        assert not (backup / "moved-sessions.jsonl").exists()
        conn = sqlite3.connect(paths["state_db"])
        title, preview = conn.execute(
            "select title, first_user_message from threads where id=?",
            ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",),
        ).fetchone()
        conn.close()
        assert len(title) > 120, "backup-only mode must not trim titles"
        assert len(preview) > 240, "backup-only mode must not trim previews"


def assert_session_alias_detection(module) -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        real_root = root / "real"
        alias_root = root / "alias"
        real_root.mkdir()
        try:
            alias_root.symlink_to(real_root, target_is_directory=True)
        except OSError:
            return

        paths = make_fake_home(real_root)
        alias_home = alias_root / ".codex"
        conn = module.sqlite_connect(alias_home / "state_5.sqlite", readonly=True)
        try:
            candidates = module.active_session_candidates(conn, alias_home, 10)
        finally:
            conn.close()
        assert len(candidates) == 1


def assert_apply_mode(module) -> None:
    with tempfile.TemporaryDirectory() as td:
        paths = make_fake_home(Path(td))
        backup = Path(td) / "backup-apply"
        args = argparse.Namespace(
            apply=True,
            backup_only=False,
            details=False,
            wait_for_codex_exit=False,
            codex_home=str(paths["codex_home"]),
            backup_root=str(backup),
            archive_older_than_days=10,
            worktree_older_than_days=7,
            rotate_logs_above_mb=0,
            thread_title_limit=120,
            thread_preview_limit=240,
            repair_thread_metadata_bloat=True,
        )
        assert module.run(args) == 0

        conn = sqlite3.connect(paths["state_db"])
        archived, archived_at, rollout_path, cwd, title, preview = conn.execute(
            "select archived, archived_at, rollout_path, cwd, title, first_user_message from threads where id=?",
            ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",),
        ).fetchone()
        conn.close()

        assert archived == 1
        assert archived_at is not None
        assert "archived_sessions" in rollout_path
        assert cwd == r"C:\DefinitelyMissingKeepCodexFast"
        assert len(title) <= 120
        assert len(preview) <= 240
        assert not paths["rollout"].exists()
        assert not paths["worktree"].exists()
        assert not paths["log_file"].exists()
        assert "DefinitelyMissingKeepCodexFast" not in (paths["codex_home"] / "config.toml").read_text(
            encoding="utf-8"
        )
        assert (backup / "restore-sessions.py").exists()
        assert (backup / "restore-thread-metadata.py").exists()
        assert (backup / "moved-sessions.jsonl").exists()
        assert (backup / "thread-metadata-repairs.jsonl").exists()
        assert (backup / "moved-worktrees.jsonl").exists()
        session_index = paths["codex_home"] / "session_index.jsonl"
        assert session_index.exists()
        assert "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" in session_index.read_text(encoding="utf-8")


def assert_normal_apply_does_not_repair_thread_metadata(module) -> None:
    with tempfile.TemporaryDirectory() as td:
        paths = make_fake_home(Path(td))
        backup = Path(td) / "backup-normal-apply"
        args = argparse.Namespace(
            apply=True,
            backup_only=False,
            details=False,
            wait_for_codex_exit=False,
            codex_home=str(paths["codex_home"]),
            backup_root=str(backup),
            archive_older_than_days=10,
            worktree_older_than_days=7,
            rotate_logs_above_mb=64,
            thread_title_limit=120,
            thread_preview_limit=240,
            repair_thread_metadata_bloat=False,
        )
        assert module.run(args) == 0

        conn = sqlite3.connect(paths["state_db"])
        title, preview = conn.execute(
            "select title, first_user_message from threads where id=?",
            ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",),
        ).fetchone()
        conn.close()

        assert len(title) > 120, "normal apply must not trim titles without explicit repair flag"
        assert len(preview) > 240, "normal apply must not trim previews without explicit repair flag"
        assert not (backup / "thread-metadata-repairs.jsonl").exists()
        assert not (backup / "restore-thread-metadata.py").exists()


def main() -> int:
    module = load_module()
    assert_top_node_processes_helper(module)
    assert_missing_codex_home_preserves_early_exit(module)
    assert_partial_required_check(module)
    assert_report_mode(module)
    assert_backup_only_mode(module)
    assert_session_alias_detection(module)
    assert_normal_apply_does_not_repair_thread_metadata(module)
    assert_apply_mode(module)
    print("smoke tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
