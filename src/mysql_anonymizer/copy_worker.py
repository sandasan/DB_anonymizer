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
    clear_destination_database_data,
    copy_database,
    drop_destination_tables,
    recreate_destination_database,
)
from .mappings import clear_mapping_store, list_mapping_fields
from .rules import migrate_identifier_rules
from .schema import inspect_schema, resolve_table_selection


class FilePauseEvent:
    def __init__(self, path: Path, parent_pid: int | None = None):
        self.path = path
        self.parent_pid = parent_pid

    def is_set(self) -> bool:
        return self.path.exists() or (
            self.parent_pid is not None and os.getppid() != self.parent_pid
        )


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
            "foreign_keys": table.get("foreign_keys", []),
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


def _database_operation(operation: str, args: dict[str, Any]) -> Any:
    """Run a short GUI database operation outside the Qt process."""
    if operation == "inspect":
        config = args["config"]
        source = connect(config["source"])
        try:
            tables = inspect_schema(source, config["source"]["database"])
            cursor = source.cursor()
            cursor.execute(
                "SELECT TABLE_NAME, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH "
                "FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE'",
                (config["source"]["database"],),
            )
            stats = {
                row[0]: (row[1], (row[2] or 0) + (row[3] or 0))
                for row in cursor.fetchall()
            }
            cursor.close()
            for table in tables:
                table["row_count"], table["size_bytes"] = stats.get(
                    table["name"], (None, 0)
                )
                table["row_count_is_estimate"] = (
                    str(table.get("engine", "")).lower() == "innodb"
                )
        finally:
            source.close()

        destination_tables: list[str] = []
        try:
            dest = connect(config["destination"])
            try:
                cursor = dest.cursor()
                cursor.execute(
                    "SELECT TABLE_NAME FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE'",
                    (config["destination"]["database"],),
                )
                destination_tables = [row[0] for row in cursor.fetchall()]
                cursor.close()
            finally:
                dest.close()
        except Exception:
            pass
        return ["inspection", tables, destination_tables, config]

    if operation == "test_connections":
        reports = []
        for role in ("source", "destination"):
            conn = None
            try:
                conn = connect(args["config"][role])
                reports.append(
                    f"✓ {role}: connected to {args['config'][role].get('database')}."
                )
            except Exception as exc:
                reports.append(f"✗ {role}: {exc}")
            finally:
                if conn is not None:
                    conn.close()
        return ["connection_report", "\n\n".join(reports)]

    if operation == "clear_destination":
        names = clear_destination_database_data(args["destination"], args["source"])
        count = clear_mapping_store(args["mapping"]) if args.get("mapping") else 0
        return ["clear", names, count]

    if operation == "discard_partial_table":
        source_settings, destination_settings = args["source"], args["destination"]
        source_identity = (str(source_settings.get("host", "localhost")).casefold(),
                           int(source_settings.get("port", 3306)),
                           str(source_settings.get("database", "")).casefold())
        destination_identity = (str(destination_settings.get("host", "localhost")).casefold(),
                                int(destination_settings.get("port", 3306)),
                                str(destination_settings.get("database", "")).casefold())
        if source_identity == destination_identity:
            raise ValueError("Refusing to remove data from the source database")
        table = args["table"]
        connection = connect(destination_settings)
        try:
            drop_destination_tables(connection, [table])
        finally:
            connection.close()
        mapping = args.get("mapping")
        if mapping:
            from .mappings import open_mapping_store
            db = open_mapping_store(mapping)
            try:
                db.execute("DELETE FROM mappings WHERE table_name=?", (table,))
                db.commit()
            finally:
                db.close()
        return ["partial_table_discarded", table]

    if operation == "browse_tables":
        role, settings = args["role"], args["settings"]
        conn = connect(settings)
        try:
            return ["browse_tables", role, inspect_schema(conn, settings["database"])]
        finally:
            conn.close()

    if operation == "browse_rows":
        role, settings = args["role"], args["settings"]
        table, columns = args["table"], args["columns"]
        conn = connect(settings)
        cursor = conn.cursor()
        try:
            conditions, params = [], []
            selected_columns = args.get("filter_columns", [])
            if any(column not in columns for column in selected_columns):
                raise ValueError("A selected filter column is not in the table")
            if args["search"]:
                conditions.append(
                    "(" + " OR ".join(
                        f"CAST(`{column.replace('`', '``')}` AS CHAR) LIKE %s"
                        for column in columns
                    ) + ")"
                )
                params.extend([f"%{args['search']}%"] * len(columns))
            if selected_columns and args["filter_value"]:
                conditions.append(
                    "(" + " OR ".join(
                        f"CAST(`{column.replace('`', '``')}` AS CHAR) LIKE %s"
                        for column in selected_columns
                    ) + ")"
                )
                params.extend([f"%{args['filter_value']}%"] * len(selected_columns))
            where = " WHERE " + " AND ".join(conditions) if conditions else ""
            result_columns = selected_columns or columns
            selected_sql = ", ".join(f"`{column.replace('`', '``')}`" for column in result_columns)
            cursor.execute(
                f"SELECT {selected_sql} FROM `{table.replace('`', '``')}`{where} LIMIT 201",
                tuple(params),
            )
            rows = [
                [None if value is None else str(value) for value in row]
                for row in cursor.fetchall()
            ]
            return ["browse_rows", table, result_columns, rows[:200]]
        finally:
            cursor.close()
            conn.close()

    raise ValueError(f"Unknown database operation: {operation}")


def run_operation(job_path: Path) -> int:
    job = load_json(str(job_path))
    result_path = Path(job["result_path"])
    error_path = Path(job["error_path"])
    activity_path = Path(job["activity_path"])
    try:
        result = _database_operation(job["operation"], job["args"])
        _write_private_json(result_path, {"kind": "operation", "result": result})
        return 0
    except Exception as exc:
        details = traceback.format_exc()
        _write_log(activity_path, f"operation_failed operation={job['operation']}")
        _write_log(error_path, f"database worker: {exc}\n{details}")
        _write_private_json(
            result_path,
            {"kind": "error", "operation": job["operation"],
             "message": str(exc), "traceback": details},
        )
        return 1


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
            if event in {"start", "table", "table_done", "range_scan"} or log_batch:
                rss_mb = _process_rss_mb()
                memory = f" rss_mb={rss_mb:.1f}" if rss_mb is not None else ""
                detail = (
                    f" event={event} table={item.get('table', '-')!r}"
                    f" copied_rows={item.get('copied_rows', 0)}"
                    f" table_rows={item.get('table_rows', 0)}"
                    f" completed_tables={item.get('completed_tables', len(state.get('completed_counts', {})))}"
                    f" total_tables={item.get('total_tables', 0)}{memory}"
                )
                if event == "range_scan":
                    detail += (f" stage={item.get('stage')} anchor={item.get('anchor_table')!r}"
                               f" scan_table={item.get('scan_table')!r}"
                               f" iteration={item.get('iteration', 0)}"
                               f" linked_tables={item.get('linked_tables', 0)}"
                               f" linked_rows={item.get('linked_rows', 0)}")
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
                FilePauseEvent(pause_path, job.get("parent_pid")),
                bool(state.get("is_resume")),
                state.get("date_range"),
                state.get("skipped_tables", []),
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
    job_path = Path(sys.argv[1])
    try:
        if load_json(str(job_path)).get("mode") == "operation":
            return run_operation(job_path)
        return run_job(job_path)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
