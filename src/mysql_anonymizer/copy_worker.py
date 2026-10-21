"""Run a GUI copy job in an isolated process, without importing Qt."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from .config import connect, load_json, save_json_atomic
from .copying import (
    CopyPaused,
    copy_database,
    drop_destination_tables,
    recreate_destination_database,
)
from .mappings import list_mapping_fields
from .rules import migrate_identifier_rules
from .schema import inspect_schema, resolve_table_selection


class FilePauseEvent:
    def __init__(self, path: Path):
        self.path = path

    def is_set(self) -> bool:
        return self.path.exists()


def _write_private_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    save_json_atomic(str(path), value)
    path.chmod(0o600)


def _write_log(path: Path, message: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    with path.open("a", encoding="utf-8") as log_file:
        os.chmod(path, 0o600)
        log_file.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {message}\n")


def _process_rss_mb() -> float | None:
    try:
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _schema_fingerprint(tables: list[dict[str, Any]]) -> str:
    structure = [
        {
            "name": table["name"],
            "engine": table.get("engine"),
            "references": sorted(table.get("references", [])),
            "external_references": sorted(table.get("external_references", [])),
            "columns": [
                {
                    key: column.get(key)
                    for key in (
                        "COLUMN_NAME",
                        "DATA_TYPE",
                        "COLUMN_TYPE",
                        "IS_NULLABLE",
                        "COLUMN_KEY",
                        "EXTRA",
                    )
                }
                for column in table.get("columns", [])
            ],
        }
        for table in tables
    ]
    return hashlib.sha256(
        json.dumps(structure, sort_keys=True, default=str).encode()
    ).hexdigest()


def run_job(job_path: Path) -> int:
    job = load_json(str(job_path))
    checkpoint_path = Path(job["checkpoint_path"])
    progress_path = Path(job["progress_path"])
    result_path = Path(job["result_path"])
    pause_path = Path(job["pause_path"])
    activity_path = Path(job["activity_path"])
    error_path = Path(job["error_path"])
    state = load_json(str(checkpoint_path))
    config, rules = state["config"], state["rules"]
    source = destination = None
    last_activity_log = time.monotonic()
    last_progress_write = 0.0
    last_logged_batch_table = None
    latest_progress: dict[str, Any] = {"event": "copy_started"}
    _write_private_json(progress_path, latest_progress)
    _write_log(activity_path, "copy_worker_started")

    try:
        source = connect(config["source"])
        tables = inspect_schema(source, config["source"]["database"])
        snapshot = job.get("inspection_snapshot")
        if not state.get("is_resume") and snapshot and (
            snapshot.get("source") != {
                key: config["source"].get(key)
                for key in ("host", "port", "database", "user")
            }
            or snapshot.get("fingerprint") != _schema_fingerprint(tables)
        ):
            raise ValueError(
                "The source schema changed since inspection. Inspect the source again before copying."
            )

        moved = migrate_identifier_rules(rules, tables)
        if moved and state.get("rules_path"):
            save_json_atomic(state["rules_path"], rules)
        action = state.get("recreate_action", "copy")
        if action == "recreate_database" and not state.get("is_resume"):
            recreate_destination_database(config["destination"], config["source"])

        destination = connect(config["destination"])
        _write_log(activity_path, f"copy_connection source_driver={type(source).__name__}")
        _write_log(activity_path, f"copy_connection destination_driver={type(destination).__name__}")

        if action == "replace_tables" and not state.get("is_resume"):
            cursor = destination.cursor()
            cursor.execute(
                "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()"
            )
            existing = {row[0] for row in cursor.fetchall()}
            cursor.close()
            resolve_table_selection(
                tables,
                state["selected"],
                state["include_dependencies"],
                existing - set(state["selected"]),
            )
            drop_destination_tables(destination, state["selected"])

        def report(item: dict[str, Any]) -> None:
            nonlocal last_activity_log, last_progress_write, last_logged_batch_table
            event = item.get("event")
            if event == "start" and not state.get("is_resume"):
                state["is_resume"] = True
                state["selected"] = item.get("selected_tables", state["selected"])
                _write_private_json(checkpoint_path, state)
            elif event == "table_done":
                counts = dict(state.get("completed_counts", {}))
                counts[item["table"]] = item["copied_rows"]
                state["completed_counts"] = counts
                _write_private_json(checkpoint_path, state)

            now = time.monotonic()
            log_batch = event == "batch" and (
                item.get("table") != last_logged_batch_table
                or now - last_activity_log >= 2
            )
            if event in {"start", "table", "table_done"} or log_batch:
                rss_mb = _process_rss_mb()
                memory = f" rss_mb={rss_mb:.1f}" if rss_mb is not None else ""
                detail = (
                    f" event={event} table={item.get('table', '-')!r}"
                    f" copied_rows={item.get('copied_rows', 0)}"
                    f" table_rows={item.get('table_rows', 0)}"
                    f" completed_tables={item.get('completed_tables', len(state.get('completed_counts', {})))}"
                    f" total_tables={item.get('total_tables', 0)}{memory}"
                )
                _write_log(activity_path, f"copy_progress{detail}")
                last_activity_log = now
                if event == "batch":
                    last_logged_batch_table = item.get("table")

            latest_progress.clear()
            latest_progress.update(item)
            if event != "batch" or now - last_progress_write >= 0.25:
                _write_private_json(progress_path, latest_progress)
                last_progress_write = now

        try:
            counts = copy_database(
                source,
                destination,
                config["source"]["database"],
                rules,
                config.get("batch_size", 2000),
                state["allow_locks"],
                state["mapping_path"],
                state["selected"],
                state["include_dependencies"],
                report,
                state.get("completed_counts", {}),
                FilePauseEvent(pause_path),
                bool(state.get("is_resume")),
            )
        except CopyPaused as paused:
            state["completed_counts"] = paused.completed_counts
            state["selected"] = paused.selected_tables
            state["is_resume"] = True
            state["paused_table"] = paused.current_table
            _write_private_json(checkpoint_path, state)
            _write_private_json(result_path, {"kind": "paused", "state": state})
            return 0

        _write_log(activity_path, f"copy_finished tables={len(counts)}")
        checkpoint_path.unlink(missing_ok=True)
        try:
            fields = list_mapping_fields(state["mapping_path"]) if state.get("mapping_path") else None
        except Exception:
            fields = None
        result = {
            "kind": "copy",
            "counts": counts,
            "fields": fields,
            "identity": [
                str(config["destination"].get("host", "localhost")).casefold(),
                str(config["destination"].get("port", 3306)),
                str(config["destination"].get("database", "")).casefold(),
            ],
        }
        _write_private_json(result_path, result)
        return 0
    except Exception as exc:
        details = traceback.format_exc()
        _write_log(activity_path, "operation_failed operation=copy")
        _write_log(error_path, f"copy worker: {exc}\n{details}")
        _write_private_json(
            result_path,
            {"kind": "error", "message": str(exc), "traceback": details},
        )
        return 1
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python -m mysql_anonymizer.copy_worker JOB.json", file=sys.stderr)
        return 2
    return run_job(Path(sys.argv[1]))


if __name__ == "__main__":
    raise SystemExit(main())
