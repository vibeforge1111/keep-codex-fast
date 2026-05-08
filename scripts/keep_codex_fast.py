#!/usr/bin/env python3
"""Backup-first Codex local-state maintenance.

Default mode is a read-only, privacy-safe report. Use --apply to archive/move/normalize.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


THREAD_ID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.I,
)
PROJECT_HEADER_RE = re.compile(r"^\[projects\.([\"'])(.+)\1\]\s*$")
TEMP_PROJECT_RE = re.compile(
    r"(\\AppData\\Local\\Temp\\|/AppData/Local/Temp/|\\Temp\\codex-|/Temp/codex-|\\Temp\\spark-|/Temp/spark-)",
    re.I,
)
DEFAULT_TITLE_LIMIT = 120
DEFAULT_PREVIEW_LIMIT = 240


@dataclass
class SessionCandidate:
    size: int
    thread_id: str
    title: str
    source: Path
    relative: Path
    created_at: int | None
    updated_at: int | None
    reason: str


@dataclass
class ThreadMetadataRepair:
    thread_id: str
    old_title: str
    new_title: str
    old_preview: str
    new_preview: str


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def codex_home_from_args(value: str | None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    override = os.environ.get("CODEX_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".codex"


def documents_backup_root() -> Path:
    docs = Path.home() / "Documents" / "Codex" / "codex-backups"
    if docs.parent.exists() or platform.system() == "Windows":
        return docs
    return Path.home() / ".codex" / "backups"


def size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return total


def gb(value: int) -> str:
    return f"{value / 1024 / 1024 / 1024:.3f}"


def mb(value: int) -> str:
    return f"{value / 1024 / 1024:.1f}"


def report(line: str) -> None:
    print(line)


def sqlite_connect(path: Path, *, readonly: bool) -> sqlite3.Connection:
    if readonly:
        return sqlite3.connect(f"{canonical_path(path).as_uri()}?mode=ro", uri=True)
    return sqlite3.connect(path)


def canonical_path(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        return path.absolute()


def codex_processes_running() -> list[str]:
    system = platform.system()
    try:
        if system == "Windows":
            output = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_Process | Select-Object Name,ProcessId,CommandLine | ConvertTo-Json -Compress"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            if not output.strip():
                return []
            data = json.loads(output)
            rows = data if isinstance(data, list) else [data]
            hits = []
            for row in rows:
                name = str(row.get("Name") or "")
                cmd = str(row.get("CommandLine") or "")
                pid = row.get("ProcessId")
                if name == "Codex.exe" or (name == "codex.exe" and ("app-server" in cmd or "OpenAI.Codex" in cmd)):
                    hits.append(f"{pid} {name}")
            return hits
        output = subprocess.check_output(["ps", "-axo", "pid=,comm=,args="], text=True)
        hits = []
        for line in output.splitlines():
            lower = line.lower()
            if "codex" in lower and ("app-server" in lower or "openai.codex" in lower or "codex desktop" in lower):
                hits.append(line.strip())
        return hits
    except Exception:
        return []


def wait_for_codex_exit() -> None:
    while codex_processes_running():
        time.sleep(2)


def sqlite_backup(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite_connect(src, readonly=True)
    target = sqlite3.connect(dst)
    source.backup(target)
    target.close()
    source.close()


def copy_if_exists(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(
            src,
            dst,
            ignore=shutil.ignore_patterns(
                "node_modules",
                ".git",
                ".next",
                "dist",
                "build",
                ".venv",
                "__pycache__",
                ".pytest_cache",
            ),
            dirs_exist_ok=True,
        )
    else:
        shutil.copy2(src, dst)
    report(f"backed_up {src.name}")


def backup_metadata(codex_home: Path, backup_root: Path) -> None:
    backup_root.mkdir(parents=True, exist_ok=True)
    for name in [
        ".codex-global-state.json",
        "config.toml",
        "history.jsonl",
        "installation_id",
        "models_cache.json",
        "session_index.jsonl",
        "version.json",
        "memories",
        "skills",
        "rules",
        "plugins",
        "automations",
    ]:
        copy_if_exists(codex_home / name, backup_root / name)
    sqlite_backup(codex_home / "state_5.sqlite", backup_root / "state_5.sqlite")


def load_pinned(codex_home: Path) -> set[str]:
    path = codex_home / ".codex-global-state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data.get("pinned-thread-ids", []))
    except Exception:
        return set()


def normalize_extended_path(value: str) -> str:
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f'pragma table_info("{table}")').fetchall()}
    except sqlite3.Error:
        return set()


def has_threads_columns(conn: sqlite3.Connection, required: set[str]) -> bool:
    return required.issubset(table_columns(conn, "threads"))


def bounded_text(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3].rstrip() + "..."


def append_session_index_name(codex_home: Path, thread_id: str, name: str) -> None:
    path = codex_home / "session_index.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "id": thread_id,
        "thread_name": name,
        "updated_at": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def report_thread_metadata_bloat(
    conn: sqlite3.Connection,
    *,
    title_limit: int,
    preview_limit: int,
) -> None:
    columns = table_columns(conn, "threads")
    if not {"id", "title"}.issubset(columns):
        report("thread_metadata_bloat skipped_missing_threads_columns")
        return
    archived_expr = "COALESCE(archived,0)=0" if "archived" in columns else "archived_at is null"
    preview_col = "first_user_message" if "first_user_message" in columns else None
    if preview_col:
        row = conn.execute(
            f"""
            select
              count(*),
              coalesce(sum(length(title)), 0),
              coalesce(sum(length(first_user_message)), 0),
              coalesce(max(length(title)), 0),
              coalesce(max(length(first_user_message)), 0),
              sum(case when length(title) > ? then 1 else 0 end),
              sum(case when length(first_user_message) > ? then 1 else 0 end),
              sum(case when length(first_user_message) > 10000 then 1 else 0 end)
            from threads
            where {archived_expr}
            """,
            (title_limit, preview_limit),
        ).fetchone()
        (
            active_rows,
            title_chars,
            preview_chars,
            max_title,
            max_preview,
            title_over_limit,
            preview_over_limit,
            preview_over_10k,
        ) = row
    else:
        row = conn.execute(
            f"""
            select
              count(*),
              coalesce(sum(length(title)), 0),
              coalesce(max(length(title)), 0),
              sum(case when length(title) > ? then 1 else 0 end)
            from threads
            where {archived_expr}
            """,
            (title_limit,),
        ).fetchone()
        active_rows, title_chars, max_title, title_over_limit = row
        preview_chars = max_preview = preview_over_limit = preview_over_10k = 0

    report(f"thread_active_rows {active_rows}")
    report(f"thread_title_chars {title_chars}")
    report(f"thread_first_user_message_chars {preview_chars}")
    report(f"thread_max_title_chars {max_title}")
    report(f"thread_max_first_user_message_chars {max_preview}")
    report(f"thread_titles_over_limit {title_over_limit or 0}")
    report(f"thread_first_user_message_over_limit {preview_over_limit or 0}")
    report(f"thread_first_user_message_over_10k {preview_over_10k or 0}")


def repair_thread_metadata_bloat(
    conn: sqlite3.Connection,
    codex_home: Path,
    backup_root: Path,
    *,
    apply: bool,
    details: bool,
    title_limit: int,
    preview_limit: int,
) -> None:
    required = {"id", "title"}
    if not has_threads_columns(conn, required):
        report("thread_metadata_repair skipped_missing_threads_columns")
        return
    columns = table_columns(conn, "threads")
    has_preview = "first_user_message" in columns
    archived_expr = "COALESCE(archived,0)=0" if "archived" in columns else "archived_at is null"
    select_preview = "first_user_message" if has_preview else "''"
    rows = conn.execute(
        f"""
        select id, title, {select_preview}
        from threads
        where {archived_expr}
          and (
            length(title) > ?
            {"or length(first_user_message) > ?" if has_preview else ""}
          )
        """,
        (title_limit, preview_limit) if has_preview else (title_limit,),
    ).fetchall()

    repairs: list[ThreadMetadataRepair] = []
    for thread_id, title, preview in rows:
        old_title = title or ""
        old_preview = preview or ""
        new_title = bounded_text(old_title, title_limit)
        new_preview = bounded_text(old_preview, preview_limit) if has_preview else ""
        if new_title != old_title or new_preview != old_preview:
            repairs.append(
                ThreadMetadataRepair(
                    str(thread_id),
                    old_title,
                    new_title,
                    old_preview,
                    new_preview,
                )
            )

    report(f"thread_metadata_repair_candidates {len(repairs)}")
    for index, item in enumerate(repairs[:10], start=1):
        label = f"thread_{index:03d}"
        title_delta = len(item.old_title) - len(item.new_title)
        preview_delta = len(item.old_preview) - len(item.new_preview)
        if details:
            report(
                f"thread_metadata_repair_candidate {label} thread_id={item.thread_id} "
                f"title_delta={title_delta} preview_delta={preview_delta}"
            )
        else:
            report(
                f"thread_metadata_repair_candidate {label} "
                f"title_delta={title_delta} preview_delta={preview_delta}"
            )

    if not apply or not repairs:
        return

    manifest = backup_root / "thread-metadata-repairs.jsonl"
    with manifest.open("w", encoding="utf-8") as handle:
        for item in repairs:
            record = {
                "thread_id": item.thread_id,
                "old_title": item.old_title,
                "new_title": item.new_title,
                "old_first_user_message": item.old_preview,
                "new_first_user_message": item.new_preview,
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    cur = conn.cursor()
    for item in repairs:
        if has_preview:
            cur.execute(
                "update threads set title=?, first_user_message=? where id=?",
                (item.new_title, item.new_preview, item.thread_id),
            )
        else:
            cur.execute(
                "update threads set title=? where id=?",
                (item.new_title, item.thread_id),
            )
        if item.new_title and item.new_title != item.old_title:
            append_session_index_name(codex_home, item.thread_id, item.new_title)
    report("thread_metadata_repair applied")
    report(f"thread_metadata_repair_manifest {manifest}")
    write_thread_metadata_restore_script(manifest, codex_home / "state_5.sqlite", backup_root)


def write_thread_metadata_restore_script(manifest: Path, state_db: Path, backup_root: Path) -> None:
    restore = backup_root / "restore-thread-metadata.py"
    restore.write_text(
        f'''import json
import sqlite3
from pathlib import Path

manifest = Path(r"{manifest}")
db = Path(r"{state_db}")
conn = sqlite3.connect(db)
conn.execute("pragma busy_timeout=10000")
cols = {{row[1] for row in conn.execute('pragma table_info("threads")').fetchall()}}
has_preview = "first_user_message" in cols
for line in manifest.read_text(encoding="utf-8").splitlines():
    rec = json.loads(line)
    if has_preview:
        conn.execute(
            "update threads set title=?, first_user_message=? where id=?",
            (rec["old_title"], rec["old_first_user_message"], rec["thread_id"]),
        )
    else:
        conn.execute(
            "update threads set title=? where id=?",
            (rec["old_title"], rec["thread_id"]),
        )
conn.commit()
conn.close()
''',
        encoding="utf-8",
    )
    report(f"thread_metadata_restore_script {restore}")


def normalize_sqlite_paths(conn: sqlite3.Connection, apply: bool) -> int:
    cur = conn.cursor()
    total = 0
    tables = [
        row[0]
        for row in cur.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
        )
    ]
    for table in tables:
        cols = cur.execute(f'pragma table_info("{table}")').fetchall()
        text_cols = [col[1] for col in cols if "TEXT" in (col[2] or "").upper() or col[2] == ""]
        for col in text_cols:
            rows = cur.execute(
                f'select rowid, "{col}" from "{table}" where "{col}" like ?',
                ("\\\\?\\%",),
            ).fetchall()
            changed = 0
            for rowid, value in rows:
                if isinstance(value, str) and value.startswith("\\\\?\\"):
                    changed += 1
                    if apply:
                        cur.execute(
                            f'update "{table}" set "{col}"=? where rowid=?',
                            (normalize_extended_path(value), rowid),
                        )
            if changed:
                report(f"extended_paths {table}.{col} {changed}")
                total += changed
    if total == 0:
        report("extended_paths 0")
    return total


def active_session_candidates(
    conn: sqlite3.Connection,
    codex_home: Path,
    archive_older_than_days: int,
    archive_age_field: str,
    archive_thread_ids: list[str],
    archive_rollout_paths: list[str],
) -> list[SessionCandidate]:
    sessions_root = codex_home / "sessions"
    sessions_root_canonical = canonical_path(sessions_root)
    cutoff = int((datetime.now() - timedelta(days=archive_older_than_days)).timestamp())
    pinned = load_pinned(codex_home)
    target_thread_ids = {value.lower() for value in archive_thread_ids}
    target_rollout_paths = {str(canonical_path(Path(value))) for value in archive_rollout_paths}
    rows = conn.execute(
        "select id, title, rollout_path, created_at, updated_at from threads where archived_at is null"
    ).fetchall()
    candidates_by_id: dict[str, SessionCandidate] = {}
    found_thread_ids: set[str] = set()
    found_rollout_paths: set[str] = set()
    for thread_id, title, rollout_path, created_at, updated_at in rows:
        thread_id = str(thread_id)
        if not rollout_path:
            continue
        source = Path(rollout_path)
        canonical_source = str(canonical_path(source))
        targeted_by_thread = thread_id.lower() in target_thread_ids
        targeted_by_path = canonical_source in target_rollout_paths
        if targeted_by_thread:
            found_thread_ids.add(thread_id.lower())
        if targeted_by_path:
            found_rollout_paths.add(canonical_source)
        if thread_id in pinned:
            if targeted_by_thread or targeted_by_path:
                report(f"targeted_session_skipped_pinned thread_id={thread_id}")
            continue
        age_value = created_at if archive_age_field == "created_at" else updated_at
        reason = f"{archive_age_field}_older_than_{archive_older_than_days}d"
        if updated_at is not None and int(updated_at) >= cutoff:
            if not targeted_by_thread and not targeted_by_path and archive_age_field == "updated_at":
                continue
        if age_value is not None and int(age_value) >= cutoff and not targeted_by_thread and not targeted_by_path:
            continue
        if targeted_by_thread:
            reason = "target_thread_id"
        elif targeted_by_path:
            reason = "target_rollout_path"
        if not source.exists():
            if targeted_by_thread or targeted_by_path:
                report(f"targeted_session_skipped_missing_rollout thread_id={thread_id}")
            continue
        try:
            relative = canonical_path(source).relative_to(sessions_root_canonical)
        except ValueError:
            if targeted_by_thread or targeted_by_path:
                report(f"targeted_session_skipped_outside_sessions thread_id={thread_id}")
            continue
        candidates_by_id[thread_id] = SessionCandidate(
            source.stat().st_size,
            thread_id,
            title or "",
            source,
            relative,
            created_at,
            updated_at,
            reason,
        )
    for target in sorted(target_thread_ids - found_thread_ids):
        report(f"targeted_session_missing thread_id={target}")
    for target in sorted(target_rollout_paths - found_rollout_paths):
        report(f"targeted_session_missing rollout_path={target}")
    candidates = list(candidates_by_id.values())
    candidates.sort(key=lambda item: item.size, reverse=True)
    return candidates


def archive_sessions(
    conn: sqlite3.Connection,
    candidates: list[SessionCandidate],
    codex_home: Path,
    backup_root: Path,
    stamp: str,
    apply: bool,
    details: bool,
) -> None:
    total = sum(item.size for item in candidates)
    report(f"old_session_candidates {len(candidates)}")
    report(f"old_session_candidate_gb {gb(total)}")
    for index, item in enumerate(candidates[:10], start=1):
        label = f"session_{index:03d}"
        if details:
            report(
                f"large_session_mb {mb(item.size)} {label} thread_id={item.thread_id} "
                f"reason={item.reason} title={item.title[:70]}"
            )
        else:
            report(f"large_session_mb {mb(item.size)} {label}")
    if not apply or not candidates:
        return

    archive_root = codex_home / "archived_sessions" / f"keep-codex-fast-{stamp}"
    manifest = backup_root / "moved-sessions.jsonl"
    archive_root.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    cur = conn.cursor()
    with manifest.open("w", encoding="utf-8") as handle:
        for item in candidates:
            dest = archive_root / item.relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(item.source), str(dest))
            record = {
                "thread_id": item.thread_id,
                "bytes": item.size,
                "from": str(item.source),
                "to": str(dest),
                "reason": item.reason,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            cur.execute(
                "update threads set rollout_path=?, archived=1, archived_at=? where id=?",
                (str(dest), now, item.thread_id),
            )
    write_session_restore_script(manifest, codex_home / "state_5.sqlite", backup_root)
    report(f"archived_sessions_root {archive_root}")
    report(f"archived_sessions_manifest {manifest}")


def write_session_restore_script(manifest: Path, state_db: Path, backup_root: Path) -> None:
    restore = backup_root / "restore-sessions.py"
    restore.write_text(
        f'''import json
import shutil
import sqlite3
from pathlib import Path

manifest = Path(r"{manifest}")
db = Path(r"{state_db}")
conn = sqlite3.connect(db)
conn.execute("pragma busy_timeout=10000")
for line in manifest.read_text(encoding="utf-8").splitlines():
    rec = json.loads(line)
    src = Path(rec["to"])
    dest = Path(rec["from"])
    if src.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
    if rec.get("thread_id"):
        conn.execute(
            "update threads set rollout_path=?, archived=0, archived_at=NULL where id=?",
            (str(dest), rec["thread_id"]),
        )
conn.commit()
conn.close()
''',
        encoding="utf-8",
    )
    report(f"session_restore_script {restore}")


def prune_config(codex_home: Path, backup_root: Path, apply: bool, write_artifacts: bool) -> None:
    path = codex_home / "config.toml"
    if not path.exists():
        report("config_prune_candidates 0")
        return
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    out: list[str] = []
    removed: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        match = PROJECT_HEADER_RE.match(line)
        if not match:
            out.append(line)
            i += 1
            continue
        project_path = match.group(2)
        block = [line]
        i += 1
        while i < len(lines) and not lines[i].startswith("["):
            block.append(lines[i])
            i += 1
        should_remove = bool(TEMP_PROJECT_RE.search(project_path)) or not Path(project_path).exists()
        if should_remove:
            removed.append(project_path)
        else:
            out.extend(block)

    if write_artifacts:
        (backup_root / "pruned-projects.txt").write_text(
            "\n".join(removed) + ("\n" if removed else ""),
            encoding="utf-8",
        )
    report(f"config_prune_candidates {len(removed)}")
    if apply and removed:
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        report("config_pruned applied")


def move_stale_worktrees(codex_home: Path, backup_root: Path, days: int, stamp: str, apply: bool) -> None:
    root = codex_home / "worktrees"
    if not root.exists():
        report("worktree_candidates 0")
        return
    cutoff = time.time() - days * 24 * 60 * 60
    candidates = [path for path in root.iterdir() if path.is_dir() and path.stat().st_mtime < cutoff]
    total = sum(size_bytes(path) for path in candidates)
    report(f"worktree_candidates {len(candidates)}")
    report(f"worktree_candidate_gb {gb(total)}")
    if not apply or not candidates:
        return
    archive_root = codex_home / "archived_worktrees" / f"keep-codex-fast-{stamp}"
    manifest = backup_root / "moved-worktrees.jsonl"
    archive_root.mkdir(parents=True, exist_ok=True)
    with manifest.open("w", encoding="utf-8") as handle:
        for source in candidates:
            dest = archive_root / source.name
            item_size = size_bytes(source)
            shutil.move(str(source), str(dest))
            handle.write(json.dumps({"from": str(source), "to": str(dest), "bytes": item_size}) + "\n")
    report(f"worktree_archive_root {archive_root}")
    report(f"worktree_manifest {manifest}")


def rotate_logs(codex_home: Path, threshold_mb: int, stamp: str, apply: bool) -> None:
    files = [path for path in codex_home.glob("logs_2.sqlite*") if path.is_file()]
    total = sum(path.stat().st_size for path in files)
    report(f"logs_mb {mb(total)}")
    if total < threshold_mb * 1024 * 1024:
        report("logs_rotate skipped_below_threshold")
        return
    if apply and files:
        archive_root = codex_home / "archived_logs" / f"keep-codex-fast-{stamp}"
        archive_root.mkdir(parents=True, exist_ok=True)
        for path in files:
            shutil.move(str(path), str(archive_root / path.name))
        report(f"logs_archive_root {archive_root}")


def top_node_processes(details: bool) -> None:
    system = platform.system()
    report("top_node_processes")
    try:
        if system == "Windows":
            command = (
                "Get-Process node -ErrorAction SilentlyContinue | "
                "Sort-Object WorkingSet64 -Descending | Select-Object -First 10 "
                "Id,ProcessName,@{n='MB';e={[math]::Round($_.WorkingSet64/1MB,1)}},Path | "
                "ConvertTo-Json -Compress"
            )
            output = subprocess.check_output(["powershell", "-NoProfile", "-Command", command], text=True)
            if not output.strip():
                return
            data = json.loads(output)
            rows = data if isinstance(data, list) else [data]
            for row in rows:
                if details:
                    report(f"node_mb {row.get('MB')} pid={row.get('Id')} path={row.get('Path')}")
                else:
                    report(f"node_mb {row.get('MB')} process=node")
            return
        output = subprocess.check_output(["ps", "-axo", "pid=,rss=,comm=,args="], text=True)
        rows = []
        for line in output.splitlines():
            parts = line.strip().split(None, 3)
            if len(parts) >= 3 and "node" in parts[2].lower():
                rows.append((int(parts[1]), line.strip()))
        for rss, line in sorted(rows, reverse=True)[:10]:
            if details:
                report(f"node_mb {rss / 1024:.1f} {line}")
            else:
                report(f"node_mb {rss / 1024:.1f} process=node")
    except Exception as exc:
        report(f"node_process_report_skipped {exc}")


def verify_sizes(codex_home: Path) -> None:
    for rel in ["sessions", "archived_sessions", "worktrees", "archived_worktrees", "archived_logs"]:
        path = codex_home / rel
        if path.exists():
            report(f"size_{rel}_gb {gb(size_bytes(path))}")


def run(args: argparse.Namespace) -> int:
    codex_home = codex_home_from_args(args.codex_home)
    if not codex_home.exists():
        report(f"codex_home_missing {codex_home}")
        return 2

    stamp = now_stamp()
    backup_root = Path(args.backup_root).expanduser() if args.backup_root else documents_backup_root() / f"keep-codex-fast-{stamp}"
    backup_root = backup_root.resolve()

    running = codex_processes_running()
    if args.apply and running and args.wait_for_codex_exit:
        report("waiting_for_codex_exit")
        wait_for_codex_exit()
        running = []

    effective_apply = bool(args.apply and not running)
    effective_backup = bool(effective_apply or args.backup_only)
    requested_mode = "apply" if args.apply else "backup-only" if args.backup_only else "report"
    effective_mode = "apply" if effective_apply else "backup-only" if effective_backup else "report"
    if args.details:
        report(f"codex_home {codex_home}")
        if effective_backup:
            report(f"backup_root {backup_root}")
    elif effective_backup:
        report(f"backup_root {backup_root}")
    report(f"requested_mode {requested_mode}")
    report(f"effective_mode {effective_mode}")
    if effective_mode == "report":
        report("mode_safety read_only=true privacy=pseudonymous")
    elif effective_mode == "backup-only":
        report("mode_safety backup_only=true archives=false state_writes=false")
    else:
        report("mode_safety backup_first=true archive_only=true permanent_delete=false")
    if args.apply and running:
        report("apply_skipped_codex_running")
        for index, proc in enumerate(running, start=1):
            if args.details:
                report(f"blocking_process {proc}")
            else:
                report(f"blocking_process codex_process_{index:03d}")

    if effective_backup:
        backup_metadata(codex_home, backup_root)

    state_db = codex_home / "state_5.sqlite"
    if state_db.exists():
        conn = sqlite_connect(state_db, readonly=not effective_apply)
        conn.execute("pragma busy_timeout=10000")
        normalize_sqlite_paths(conn, effective_apply)
        report_thread_metadata_bloat(
            conn,
            title_limit=args.thread_title_limit,
            preview_limit=args.thread_preview_limit,
        )
        repair_thread_metadata_bloat(
            conn,
            codex_home,
            backup_root,
            apply=effective_apply and args.repair_thread_metadata_bloat,
            details=args.details,
            title_limit=args.thread_title_limit,
            preview_limit=args.thread_preview_limit,
        )
        candidates = active_session_candidates(
            conn,
            codex_home,
            args.archive_older_than_days,
            args.archive_age_field,
            args.archive_thread_id,
            args.archive_rollout_path,
        )
        archive_sessions(conn, candidates, codex_home, backup_root, stamp, effective_apply, args.details)
        if effective_apply:
            conn.commit()
            try:
                conn.execute("pragma wal_checkpoint(truncate)")
            except Exception as exc:
                report(f"wal_checkpoint_skipped {exc}")
            try:
                conn.execute("pragma optimize")
            except Exception as exc:
                report(f"sqlite_optimize_skipped {exc}")
        conn.close()
    else:
        report("state_db_missing")

    prune_config(codex_home, backup_root, effective_apply, effective_backup)
    move_stale_worktrees(codex_home, backup_root, args.worktree_older_than_days, stamp, effective_apply)
    rotate_logs(codex_home, args.rotate_logs_above_mb, stamp, effective_apply)
    verify_sizes(codex_home)
    top_node_processes(args.details)
    report("done")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safe, backup-first, archive-only Codex local-state maintenance."
    )
    parser.add_argument("--apply", action="store_true", help="Apply maintenance actions. Default is report-only.")
    parser.add_argument(
        "--backup-only",
        action="store_true",
        help="Create backups without applying maintenance actions. Default report mode writes no files.",
    )
    parser.add_argument(
        "--details",
        action="store_true",
        help="Include raw thread IDs, titles, paths, and process paths in output.",
    )
    parser.add_argument("--wait-for-codex-exit", action="store_true", help="Wait until Codex exits before applying.")
    parser.add_argument("--codex-home", help="Override Codex home. Defaults to CODEX_HOME or ~/.codex.")
    parser.add_argument("--backup-root", help="Override backup output folder.")
    parser.add_argument("--archive-older-than-days", type=int, default=10)
    parser.add_argument(
        "--archive-age-field",
        choices=["updated_at", "created_at"],
        default="updated_at",
        help="Timestamp field used by --archive-older-than-days. Defaults to updated_at.",
    )
    parser.add_argument(
        "--archive-thread-id",
        action="append",
        default=[],
        help="Archive one active session by thread id, bypassing the age threshold. Can be repeated.",
    )
    parser.add_argument(
        "--archive-rollout-path",
        action="append",
        default=[],
        help="Archive one active session by rollout JSONL path, bypassing the age threshold. Can be repeated.",
    )
    parser.add_argument("--worktree-older-than-days", type=int, default=7)
    parser.add_argument("--rotate-logs-above-mb", type=int, default=64)
    parser.add_argument(
        "--thread-title-limit",
        type=int,
        default=DEFAULT_TITLE_LIMIT,
        help="Title length threshold for metadata-bloat reporting and optional repair.",
    )
    parser.add_argument(
        "--thread-preview-limit",
        type=int,
        default=DEFAULT_PREVIEW_LIMIT,
        help="Preview length threshold for metadata-bloat reporting and optional repair.",
    )
    parser.add_argument(
        "--repair-thread-metadata-bloat",
        action="store_true",
        help="With --apply, trim oversized thread title/preview metadata. Default --apply only reports candidates.",
    )
    args = parser.parse_args(argv)
    if args.apply and args.backup_only:
        parser.error("--apply and --backup-only cannot be used together")
    for thread_id in args.archive_thread_id:
        if not THREAD_ID_RE.fullmatch(thread_id):
            parser.error(f"--archive-thread-id must be a UUID-like thread id: {thread_id}")
    if args.archive_older_than_days < 0:
        parser.error("--archive-older-than-days must be non-negative")
    if args.thread_title_limit < 20:
        parser.error("--thread-title-limit must be at least 20")
    if args.thread_preview_limit < args.thread_title_limit:
        parser.error("--thread-preview-limit must be greater than or equal to --thread-title-limit")
    return args


if __name__ == "__main__":
    raise SystemExit(run(parse_args(sys.argv[1:])))
