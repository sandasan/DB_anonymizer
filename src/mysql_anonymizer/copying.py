from __future__ import annotations

import re
from typing import Any, Callable

import mysql.connector

from .config import connect
from .mappings import open_mapping_store
from .rules import validate_rules
from .schema import inspect_schema, qname, resolve_table_selection
from .transformations import text_column_length, transform, unique_text


class CopyPaused(Exception):
    """Raised at a committed batch boundary when a copy is paused."""

    def __init__(self, completed_counts: dict[str, int], current_table: str | None,
                 selected_tables: list[str]):
        super().__init__("Copy paused")
        self.completed_counts = dict(completed_counts)
        self.current_table = current_table
        self.selected_tables = list(selected_tables)


def _quote_identifier(identifier: str) -> str:
    return f"`{identifier.replace('`', '``')}`"


def _close_cursor_safely(connection, cursor) -> None:
    try:
        cursor.close()
    except Exception:
        try:
            connection.consume_results()
        except Exception:
            pass
        try:
            cursor.close()
        except Exception:
            pass


def _estimated_sql_row_size(row: tuple[Any, ...], column_count: int) -> int:
    """Conservatively estimate a row's size after SQL literal escaping."""
    value_bytes = 0
    for value in row:
        if value is None:
            continue
        if isinstance(value, bytes):
            size = len(value)
        elif isinstance(value, str):
            size = len(value.encode("utf-8"))
        else:
            size = len(str(value).encode("utf-8"))
        # Quotes and backslashes can be escaped, roughly doubling string size.
        value_bytes += size * 2
    return value_bytes + column_count * 16 + 128


def _executemany_with_packet_limit(cursor, statement: str, rows: list[tuple[Any, ...]],
                                  max_allowed_packet: int, column_count: int) -> None:
    """Split inserts so each generated multi-row statement stays below MySQL's packet limit."""
    # Leave headroom for statement syntax and differences between this estimate
    # and mysql-connector's escaping/encoding.
    budget = max(1, int(max_allowed_packet * 0.7))
    chunk: list[tuple[Any, ...]] = []
    chunk_size = 0
    for row in rows:
        row_size = _estimated_sql_row_size(row, column_count)
        if row_size > budget:
            if chunk:
                cursor.executemany(statement, chunk)
                chunk = []
                chunk_size = 0
            # Try the row by itself: estimates are intentionally conservative, so
            # it may still fit. If it does not, MySQL returns the usual packet error.
            cursor.executemany(statement, [row])
            continue
        if chunk and chunk_size + row_size > budget:
            cursor.executemany(statement, chunk)
            chunk = []
            chunk_size = 0
        chunk.append(row)
        chunk_size += row_size
    if chunk:
        cursor.executemany(statement, chunk)


def drop_destination_tables(destination, table_names: list[str]) -> None:
    """Drop selected destination tables before recreating them from source."""
    cursor = destination.cursor()
    try:
        cursor.execute("SET FOREIGN_KEY_CHECKS=0")
        if table_names:
            quoted_tables = ", ".join(_quote_identifier(table) for table in table_names)
            cursor.execute(f"DROP TABLE IF EXISTS {quoted_tables}")
    finally:
        try:
            cursor.execute("SET FOREIGN_KEY_CHECKS=1")
        except Exception:
            try:
                destination.consume_results()
                cursor.execute("SET FOREIGN_KEY_CHECKS=1")
            except Exception:
                pass
        finally:
            _close_cursor_safely(destination, cursor)


def clear_destination_database_data(settings: dict[str, Any], source_settings: dict[str, Any]) -> list[str]:
    """Drop all base tables in the destination schema without dropping the schema."""
    database = settings.get("database")
    if not database:
        raise ValueError("Destination database is required")
    source_identity = (
        str(source_settings.get("host", "localhost")).casefold(),
        int(source_settings.get("port", 3306)),
        str(source_settings.get("database", "")).casefold(),
    )
    destination_identity = (
        str(settings.get("host", "localhost")).casefold(),
        int(settings.get("port", 3306)),
        str(database).casefold(),
    )
    if source_identity == destination_identity:
        raise ValueError("Refusing to clear the source database")

    connection = connect(settings)
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT TABLE_NAME FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE'",
                (database,),
            )
            table_names = [row[0] for row in cursor.fetchall()]
        finally:
            cursor.close()
        drop_destination_tables(connection, table_names)
        return table_names
    finally:
        connection.close()


def recreate_destination_database(settings: dict[str, Any], source_settings: dict[str, Any]) -> None:
    """Drop and recreate the configured destination schema using server defaults."""
    database = settings.get("database")
    if not database:
        raise ValueError("Destination database is required")
    source_identity = (
        str(source_settings.get("host", "localhost")).lower(),
        int(source_settings.get("port", 3306)),
        str(source_settings.get("database", "")).casefold(),
    )
    destination_identity = (
        str(settings.get("host", "localhost")).lower(),
        int(settings.get("port", 3306)),
        str(database).casefold(),
    )
    if source_identity == destination_identity:
        raise ValueError("Refusing to drop the source database")

    admin_settings = dict(settings)
    admin_settings.pop("database", None)
    connection = connect(admin_settings)
    cursor = connection.cursor()
    quoted_database = _quote_identifier(database)
    try:
        # Check CREATE permission before dropping anything. IF NOT EXISTS is a no-op
        # for an existing schema but still validates that the account may create it.
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS {quoted_database}")
        cursor.execute(
            "SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME "
            "FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s",
            (database,),
        )
        existing = cursor.fetchone()
        cursor.execute(f"DROP DATABASE IF EXISTS {quoted_database}")
        create_sql = f"CREATE DATABASE {quoted_database}"
        if existing:
            character_set, collation = existing
            create_sql += f" CHARACTER SET {_quote_identifier(character_set)} COLLATE {_quote_identifier(collation)}"
        cursor.execute(create_sql)
        connection.commit()
    finally:
        cursor.close()
        connection.close()


def copy_database(source, destination, schema: str, rules: dict[str, Any], batch_size: int = 2000,
                  allow_source_table_locks: bool = False, mapping_store: str | None = None,
                  selected_tables: list[str] | None = None,
                  include_dependencies: bool = False,
                  progress_callback: Callable[[dict[str, Any]], None] | None = None,
                  completed_counts: dict[str, int] | None = None,
                  pause_event=None,
                  resume: bool = False) -> dict[str, int]:
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError("batch_size must be a positive integer")
    all_tables = inspect_schema(source, schema)
    dst_check = destination.cursor()
    dst_check.execute("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()")
    existing_destination_tables = {row[0] for row in dst_check.fetchall()}
    dst_check.close()
    explicitly_selected = (
        set(selected_tables) if selected_tables is not None
        else {table["name"] for table in all_tables}
    )
    selected_names = set(resolve_table_selection(
        all_tables, selected_tables, include_dependencies, existing_destination_tables))
    # A table may still be selected in the UI after an earlier copy. If another
    # selected table references it, reuse the destination copy instead of treating
    # it as a collision or trying to copy it again.
    referenced_existing = {
        dependency
        for table in all_tables
        if table["name"] in selected_names
        for dependency in table.get("references", [])
        if dependency in existing_destination_tables
    }
    # Reuse existing destination tables only when they were added solely as
    # dependencies. Explicitly selected tables must remain selected so collision
    # checks (or resume handling) can report and process them correctly.
    selected_names.difference_update(referenced_existing - explicitly_selected)
    tables = [table for table in all_tables if table["name"] in selected_names]
    external_references = [
        f"{table['name']}.{reference}"
        for table in tables
        for reference in table.get("external_references", [])
    ]
    if external_references:
        raise ValueError(
            "Selected source tables have foreign keys to other schemas, which cannot be "
            "safely redirected into the destination: " + "; ".join(external_references)
        )
    table_positions = {table["name"]: index for index, table in enumerate(tables, start=1)}
    completed_counts = dict(completed_counts or {})
    completed_names = set(completed_counts)
    missing_completed = sorted(completed_names - existing_destination_tables)
    if missing_completed:
        raise ValueError("Resume checkpoint tables are missing from destination: " + ", ".join(missing_completed))
    collisions = sorted((selected_names & existing_destination_tables) - completed_names) if not resume else []
    if collisions:
        raise ValueError("Destination already contains selected table(s): " + ", ".join(collisions)
                         + ". Remove or rename them before copying")
    non_transactional = [t["name"] for t in tables if t["engine"] != "InnoDB"]
    if non_transactional and not allow_source_table_locks:
        names = ", ".join(non_transactional)
        raise ValueError(f"Non-InnoDB source tables found ({names}). Set allow_source_table_locks=true only if it is acceptable to block writes to all source tables during the copy")
    validate_rules(tables, rules)
    dst, src = destination.cursor(), source.cursor()
    counts: dict[str, int] = completed_counts
    mapping_db = open_mapping_store(mapping_store) if mapping_store else None
    source_locked = False
    try:
        dst.execute("SELECT @@max_allowed_packet")
        max_allowed_packet = int(dst.fetchone()[0])
        if mapping_db is not None:
            # Recreated tables must not retain mappings for source rows that were
            # deleted or rules that no longer apply. Completed tables in a resumed
            # copy are untouched; the interrupted table is rebuilt from row one.
            for table in tables:
                if table["name"] not in completed_names:
                    mapping_db.execute(
                        "DELETE FROM mappings WHERE table_name=?", (table["name"],)
                    )
            mapping_db.commit()
        if non_transactional:
            # MySQL releases table locks on START TRANSACTION. Disable autocommit first,
            # then hold explicit READ locks across DDL inspection and all row reads.
            src.execute("SET autocommit=0")
            lock_list = ", ".join(f"{qname(schema, t['name'])} READ" for t in tables)
            if lock_list:
                src.execute(f"LOCK TABLES {lock_list}")
                source_locked = True
        else:
            source.start_transaction(isolation_level="REPEATABLE READ", consistent_snapshot=True, readonly=True)
        row_totals: dict[str, int] = {}
        for table in tables:
            src.execute(f"SELECT COUNT(*) FROM {qname(schema, table['name'])}")
            row_totals[table["name"]] = src.fetchone()[0]
        total_rows = sum(row_totals.values())
        if progress_callback:
            progress_callback({"event": "start", "total_tables": len(tables), "total_rows": total_rows,
                               "selected_tables": [table["name"] for table in tables],
                               "completed_tables": len(counts), "completed_rows": sum(counts.values())})
        dst.execute("SET FOREIGN_KEY_CHECKS=0")
        for t in tables:
            name = t["name"]
            if name in existing_destination_tables:
                continue
            src.execute(f"SHOW CREATE TABLE {qname(schema, name)}")
            ddl = src.fetchone()[1]
            quoted = name.replace("`", "``")
            ddl = re.sub(r"^CREATE TABLE `[^`]+`", f"CREATE TABLE `{quoted}`", ddl)
            dst.execute(ddl)
        for t in tables:
            name = t["name"]
            if name in completed_names:
                continue
            if name in existing_destination_tables:
                # This table was only partly copied before the checkpoint. Restart it
                # from row one so the destination cannot contain duplicate batches.
                dst.execute(f"DELETE FROM `{name.replace('`', '``')}`")
                destination.commit()
            if progress_callback:
                progress_callback({"event": "table", "table": name,
                                   "table_index": table_positions[name], "total_tables": len(tables),
                                   "table_rows": row_totals[name], "copied_rows": 0,
                                   "total_rows": total_rows, "completed_rows": sum(counts.values())})
            cols = [c for c in t["columns"] if "GENERATED" not in c["EXTRA"].upper()]
            names = [c["COLUMN_NAME"] for c in cols]
            qcols = ",".join(f"`{c.replace('`', '``')}`" for c in names)
            src.execute(f"SELECT {qcols} FROM {qname(schema, name)}")
            transforms = rules.get("tables", {}).get(name, {}).get("anonymize", {})
            column_info = {c["COLUMN_NAME"]: c for c in cols}
            transform_columns = [
                (names.index(col), col, rule)
                for col, rule in transforms.items()
            ]
            set_indexes = [
                index for index, col in enumerate(names)
                if column_info[col]["DATA_TYPE"].lower() == "set"
            ]
            unique_state = {
                col: ({}, set())
                for col in transforms
                if column_info[col]["COLUMN_KEY"] == "UNI" or transforms[col].get("type") == "unique"
            }
            insert = f"INSERT INTO `{name.replace('`', '``')}` ({qcols}) VALUES ({','.join(['%s'] * len(names))})"
            count = 0
            while rows := src.fetchmany(batch_size):
                output = []
                mapping_rows = []
                mapping_seen: set[tuple[str, str]] = set()
                for row in rows:
                    values = list(row)
                    for index in set_indexes:
                        if isinstance(values[index], set):
                            # mysql-connector-python decodes MySQL SET fields as Python sets,
                            # while INSERT parameters require MySQL's comma-separated form.
                            values[index] = ",".join(sorted(values[index]))
                    for index, col, rule in transform_columns:
                        original_value = values[index]
                        if col in unique_state and original_value is not None and original_value in unique_state[col][0]:
                            new_value = unique_state[col][0][original_value]
                        else:
                            new_value = transform(original_value, rule, name, col)
                        if col in unique_state and new_value is not None:
                            mapping, used = unique_state[col]
                            new_value = unique_text(values[index], new_value, name, col,
                                                    text_column_length(column_info[col]["COLUMN_TYPE"]),
                                                    mapping, used)
                        values[index] = new_value
                        if mapping_db is not None and original_value is not None and new_value is not None:
                            original_text = str(original_value)
                            map_key = (col, original_text)
                            if map_key not in mapping_seen:
                                mapping_seen.add(map_key)
                                mapping_rows.append((name, col, original_text, str(new_value)))
                    output.append(tuple(values))
                try:
                    _executemany_with_packet_limit(dst, insert, output,
                                                   max_allowed_packet, len(names))
                except mysql.connector.Error as exc:
                    first_row = count + 1
                    last_row = count + len(output)
                    details = []
                    error_text = str(exc)
                    row_match = re.search(r"\bat row (\d+)\b", error_text, re.I)
                    column_match = re.search(r"\bcolumn ['`\"]([^'`\"]+)['`\"]", error_text, re.I)
                    if row_match:
                        failed_index = int(row_match.group(1)) - 1
                        if 0 <= failed_index < len(output):
                            failed_values = output[failed_index]
                            details.append(f"source row {first_row + failed_index}")
                            primary_key = [c["COLUMN_NAME"] for c in cols if c["COLUMN_KEY"] == "PRI"]
                            if primary_key:
                                key_values = {
                                    key: failed_values[names.index(key)] for key in primary_key
                                }
                                details.append(f"primary key {key_values!r}")
                            if column_match and column_match.group(1) in column_info:
                                col = column_match.group(1)
                                details.append(f"{col} value {failed_values[names.index(col)]!r}")
                    pinpointed = f" ({', '.join(details)})" if details else ""
                    raise RuntimeError(
                        f"Failed copying table {name!r}, source rows {first_row}-{last_row}"
                        f"{pinpointed}: {exc}"
                    ) from exc
                count += len(output)
                # Persist each batch so a graceful pause or application restart can
                # continue from a durable checkpoint.
                destination.commit()
                if mapping_db is not None and mapping_rows:
                    mapping_db.executemany(
                        "INSERT OR IGNORE INTO mappings VALUES (?, ?, ?, ?)", mapping_rows
                    )
                    mapping_db.commit()
                if progress_callback:
                    progress_callback({"event": "batch", "table": name,
                                       "table_index": table_positions[name], "total_tables": len(tables),
                                       "table_rows": row_totals[name], "copied_rows": count,
                                       "total_rows": total_rows,
                                       "completed_rows": sum(counts.values()) + count})
                if pause_event is not None and pause_event.is_set() and count < row_totals[name]:
                    raise CopyPaused(counts, name, [table["name"] for table in tables])
            counts[name] = count
            destination.commit()
            if mapping_db is not None:
                mapping_db.commit()
            if progress_callback:
                progress_callback({"event": "table_done", "table": name,
                                   "table_index": table_positions[name], "total_tables": len(tables),
                                   "table_rows": row_totals[name], "copied_rows": count,
                                   "total_rows": total_rows,
                                   "completed_rows": sum(counts.values()),
                                   "completed_tables": len(counts)})
            if pause_event is not None and pause_event.is_set() and len(counts) < len(tables):
                raise CopyPaused(counts, None, [table["name"] for table in tables])
        destination.commit()
        if mapping_db is not None:
            mapping_db.commit()
    except Exception:
        try:
            destination.rollback()
        except Exception:
            pass
        if mapping_db is not None:
            try:
                mapping_db.rollback()
            except Exception:
                pass
        raise
    finally:
        try:
            dst.execute("SET FOREIGN_KEY_CHECKS=1")
        except Exception:
            try:
                destination.consume_results()
                dst.execute("SET FOREIGN_KEY_CHECKS=1")
            except Exception:
                pass
        if source_locked:
            try:
                source.rollback()
            except Exception:
                pass
            try:
                src.execute("UNLOCK TABLES")
            except Exception:
                pass
        _close_cursor_safely(source, src)
        _close_cursor_safely(destination, dst)
        if mapping_db is not None:
            mapping_db.close()
        if not source_locked:
            try:
                source.rollback()
            except Exception:
                pass
    return counts
