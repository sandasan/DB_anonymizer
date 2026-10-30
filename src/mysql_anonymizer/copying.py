from __future__ import annotations

import re
from typing import Any, Callable

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


def _key_predicate(columns: list[str], count: int, qualifier: str | None = None) -> str:
    quoted = [
        f"{qualifier}.{_quote_identifier(column)}" if qualifier else _quote_identifier(column)
        for column in columns
    ]
    if len(columns) == 1:
        return f"{quoted[0]} IN ({','.join(['%s'] * count)})"
    tuple_sql = "(" + ",".join(quoted) + ")"
    placeholders = ",".join("(" + ",".join(["%s"] * len(columns)) + ")" for _ in range(count))
    return f"{tuple_sql} IN ({placeholders})"


def _related_parent_keys(cursor, schema: str, child: dict[str, Any], parent: dict[str, Any],
                         foreign_key: dict[str, Any], child_keys: set[tuple[Any, ...]] | None) -> set[tuple[Any, ...]]:
    """Find matching parent keys in SQL, optionally limited to selected child rows."""
    parent_pk = [column["COLUMN_NAME"] for column in parent["columns"]
                 if column["COLUMN_KEY"] == "PRI"]
    if not parent_pk:
        raise ValueError(
            f"Relationship-preserving date-range copy requires a primary key on related table {parent['name']!r}"
        )
    child_pk = [column["COLUMN_NAME"] for column in child["columns"]
                if column["COLUMN_KEY"] == "PRI"]
    pairs = foreign_key["columns"]
    child_qname = qname(schema, child["name"])
    parent_qname = qname(schema, parent["name"])
    if child["name"] == parent["name"]:
        # Self-referencing foreign keys need no join: the referenced keys are
        # already present on each selected child row. Avoiding a self-join also
        # keeps LOCK TABLES compatible with the unaliased source table lock.
        referenced_columns = [pair["referenced_column"] for pair in pairs]
        child_fk_columns = ",".join(_quote_identifier(pair["column"]) for pair in pairs)
        referenced_values: set[tuple[Any, ...]] = set()
        if child_keys is None:
            cursor.execute(f"SELECT DISTINCT {child_fk_columns} FROM {child_qname}")
            referenced_values.update(tuple(row) for row in cursor.fetchall()
                                     if all(value is not None for value in row))
        else:
            if not child_keys:
                return set()
            if not child_pk:
                raise ValueError(f"Selected relationship table {child['name']!r} must have a primary key")
            ordered = list(child_keys)
            for offset in range(0, len(ordered), 400):
                part = ordered[offset:offset + 400]
                where = _key_predicate(child_pk, len(part))
                params = [value for key in part for value in key] if len(child_pk) > 1 else [key[0] for key in part]
                cursor.execute(f"SELECT DISTINCT {child_fk_columns} FROM {child_qname} WHERE {where}", tuple(params))
                referenced_values.update(tuple(row) for row in cursor.fetchall()
                                         if all(value is not None for value in row))
        if not referenced_values:
            return set()
        result: set[tuple[Any, ...]] = set()
        ordered_values = list(referenced_values)
        qparent_pk = ",".join(_quote_identifier(column) for column in parent_pk)
        qreferenced = ",".join(_quote_identifier(column) for column in referenced_columns)
        for offset in range(0, len(ordered_values), 400):
            part = ordered_values[offset:offset + 400]
            where = _key_predicate(referenced_columns, len(part))
            params = [value for key in part for value in key] if len(referenced_columns) > 1 else [key[0] for key in part]
            cursor.execute(
                f"SELECT DISTINCT {qparent_pk} FROM {parent_qname} WHERE {where}",
                tuple(params),
            )
            result.update(tuple(row) for row in cursor.fetchall())
        return result

    join = " AND ".join(
        f"{child_qname}.{_quote_identifier(pair['column'])} = "
        f"{parent_qname}.{_quote_identifier(pair['referenced_column'])}"
        for pair in pairs
    )
    qparent_pk = ",".join(f"{parent_qname}.{_quote_identifier(column)}" for column in parent_pk)
    statement = (
        f"SELECT DISTINCT {qparent_pk} FROM {child_qname} JOIN {parent_qname} ON {join}"
    )
    if child_keys is None:
        cursor.execute(statement)
        return {tuple(row) for row in cursor.fetchall()}
    if not child_keys:
        return set()
    if not child_pk:
        raise ValueError(f"Selected relationship table {child['name']!r} must have a primary key")
    result: set[tuple[Any, ...]] = set()
    ordered = list(child_keys)
    for offset in range(0, len(ordered), 400):
        part = ordered[offset:offset + 400]
        where = _key_predicate(child_pk, len(part), qualifier=child_qname)
        params = [value for key in part for value in key] if len(child_pk) > 1 else [key[0] for key in part]
        cursor.execute(f"{statement} WHERE {where}", tuple(params))
        result.update(tuple(row) for row in cursor.fetchall())
    return result


def _select_related_keys(cursor, schema: str, tables: list[dict[str, Any]],
                         date_range: dict[str, Any],
                         progress_callback: Callable[[dict[str, Any]], None] | None = None) -> dict[str, set[tuple[Any, ...]]]:
    """Select checked date-range roots and recursively referenced parent rows."""
    by_name = {table["name"]: table for table in tables}
    temporal_types = {"date", "datetime", "timestamp"}
    date_table_names = list(dict.fromkeys(date_range.get("date_tables", [])))
    anchor_label = ", ".join(date_table_names) if date_table_names else "checked tables"
    start, end = date_range.get("start"), date_range.get("end")
    if not start or not end or start > end:
        raise ValueError("Date range requires valid start and end values, with start no later than end")
    selected: dict[str, set[tuple[Any, ...]]] = {}
    frontier: dict[str, set[tuple[Any, ...]]] = {}
    if progress_callback:
        progress_callback({"event": "range_scan", "stage": "date_tables", "anchor_table": anchor_label,
                           "iteration": 0, "linked_tables": 0, "linked_rows": 0})
    for table_name in date_table_names:
        table = by_name.get(table_name)
        if table is None:
            raise ValueError(f"Unknown date-range table: {table_name!r}")
        table_pk = [item["COLUMN_NAME"] for item in table["columns"] if item["COLUMN_KEY"] == "PRI"]
        if not table_pk:
            raise ValueError(f"Date-range table {table_name!r} must have a primary key")
        temporal_columns = [item["COLUMN_NAME"] for item in table["columns"]
                            if item.get("DATA_TYPE", "").lower() in temporal_types]
        legacy_columns = date_range.get("legacy_date_columns", {}).get(table_name)
        if legacy_columns:
            temporal_columns = [name for name in temporal_columns if name in legacy_columns]
        if not temporal_columns:
            raise ValueError(f"Selected date-range table {table_name!r} has no date/time columns")
        qtable_pk = ",".join(f"`{key.replace('`', '``')}`" for key in table_pk)
        conditions = " OR ".join(
            f"`{temporal_column.replace('`', '``')}` >= %s AND "
            f"`{temporal_column.replace('`', '``')}` <= %s"
            for temporal_column in temporal_columns
        )
        params = tuple(value for _ in temporal_columns for value in (start, end))
        cursor.execute(f"SELECT {qtable_pk} FROM {qname(schema, table_name)} WHERE ({conditions})", params)
        keys = {tuple(row) for row in cursor.fetchall()}
        selected[table_name] = keys
        frontier[table_name] = set(keys)
        if progress_callback:
            progress_callback({"event": "range_scan", "stage": "date_table", "anchor_table": anchor_label,
                               "scan_table": table_name, "iteration": 0,
                               "linked_tables": len(selected),
                               "linked_rows": sum(len(rows) for rows in selected.values())})
    full_scan_pending: set[str] = set()
    for table_name in list(dict.fromkeys(date_range.get("full_tables", []))):
        if table_name in selected:
            continue
        table = by_name.get(table_name)
        if table is None:
            raise ValueError(f"Unknown full-copy date-range table: {table_name!r}")
        # Full-copy roots are streamed during table copy instead of materializing
        # every primary key in Python. Their FK values are scanned once to include parents.
        selected[table_name] = set()
        full_scan_pending.add(table_name)
        if progress_callback:
            progress_callback({"event": "range_scan", "stage": "full_table", "anchor_table": anchor_label,
                               "scan_table": table_name, "iteration": 0,
                               "linked_tables": len(selected),
                               "linked_rows": sum(len(rows) for rows in selected.values())})

    iteration = 0
    if progress_callback:
        progress_callback({"event": "range_scan", "stage": "date_tables", "anchor_table": anchor_label,
                           "iteration": iteration, "linked_tables": len(selected),
                           "linked_rows": sum(len(rows) for rows in selected.values())})
    while frontier or full_scan_pending:
        iteration += 1
        additions: dict[str, set[tuple[Any, ...]]] = {}
        # Process only the tables discovered in the previous pass (plus full-copy
        # roots), rather than walking every table in the schema on every pass.
        child_names = list(dict.fromkeys([*frontier, *full_scan_pending]))
        for child_name in child_names:
            child = by_name[child_name]
            for foreign_key in child.get("foreign_keys", []):
                parent_name = foreign_key["referenced_table"]
                parent = by_name.get(parent_name)
                if parent is None:
                    continue
                child_keys = None if child_name in full_scan_pending else frontier.get(child_name, set())
                if progress_callback:
                    progress_callback({"event": "range_scan", "stage": "relationship_query",
                                       "anchor_table": anchor_label, "scan_table": child_name,
                                       "iteration": iteration, "linked_tables": len(selected),
                                       "linked_rows": sum(len(rows) for rows in selected.values())})
                additions.setdefault(parent_name, set()).update(
                    _related_parent_keys(cursor, schema, child, parent, foreign_key, child_keys)
                )
        full_scan_pending.clear()

        # These are FK parents of selected child rows. Walking only toward parents
        # preserves their references without expanding into the entire child graph.
        frontier = {}
        for table_name, keys in additions.items():
            fresh = keys - selected.get(table_name, set())
            if fresh:
                selected.setdefault(table_name, set()).update(fresh)
                frontier[table_name] = fresh
        if progress_callback:
            progress_callback({"event": "range_scan", "stage": "relationships", "anchor_table": anchor_label,
                               "iteration": iteration, "linked_tables": len(selected),
                               "linked_rows": sum(len(keys) for keys in selected.values())})
    return selected


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
                  resume: bool = False,
                  date_range: dict[str, Any] | None = None,
                  skipped_tables: list[str] | None = None) -> dict[str, int]:
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError("batch_size must be a positive integer")
    all_tables = inspect_schema(source, schema)
    if date_range and "date_tables" not in date_range:
        # Older checkpoints stored one Main table plus additional dated tables.
        # Translate them so an interrupted copy can still resume after the UI change.
        date_range = dict(date_range)
        legacy_main = date_range.pop("table", None)
        legacy_additional = date_range.pop("additional_tables", [])
        date_range["date_tables"] = list(dict.fromkeys(
            ([legacy_main] if legacy_main else []) + list(legacy_additional)
        ))
        legacy_column = date_range.pop("column", None)
        if legacy_main and legacy_column:
            date_range["legacy_date_columns"] = {legacy_main: [legacy_column]}
    if date_range:
        # A range-filtered table needs a stable key for row selection. If a
        # selected temporal table has no primary key, preserve it by copying it
        # in full, as we do for selected tables without temporal columns.
        date_range = dict(date_range)
        date_tables = list(date_range.get("date_tables", []))
        full_tables = list(date_range.get("full_tables", []))
        by_name = {table["name"]: table for table in all_tables}
        full_copy_temporal = {
            name for name in date_tables
            if not any(column.get("COLUMN_KEY") == "PRI"
                       for column in by_name.get(name, {}).get("columns", []))
        }
        date_range["date_tables"] = [name for name in date_tables if name not in full_copy_temporal]
        date_range["full_tables"] = list(dict.fromkeys(full_tables + [
            name for name in date_tables if name in full_copy_temporal
        ]))
    dst_check = destination.cursor()
    dst_check.execute("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()")
    existing_destination_tables = {row[0] for row in dst_check.fetchall()}
    dst_check.close()
    explicitly_selected = (
        set(selected_tables) if selected_tables is not None
        else {table["name"] for table in all_tables}
    )
    skipped_tables = set(skipped_tables or [])
    if date_range:
        roots = {*date_range.get("date_tables", []), *date_range.get("full_tables", [])}
        if not roots.issubset(explicitly_selected):
            raise ValueError("Every date-range anchor table must be selected for copying")
        selected_names = roots
    else:
        selected_names = set(resolve_table_selection(
            all_tables, selected_tables, include_dependencies, existing_destination_tables))
    if not date_range:
        selected_names.difference_update(skipped_tables)
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
    if not date_range:
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
    if collisions and not date_range:
        raise ValueError("Destination already contains selected table(s): " + ", ".join(collisions)
                         + ". Remove or rename them before copying")
    non_transactional = [
        t["name"] for t in (all_tables if date_range else tables)
        if t["engine"] != "InnoDB"
    ]
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
        if non_transactional:
            # MySQL releases table locks on START TRANSACTION. Disable autocommit first,
            # then hold explicit READ locks across DDL inspection and all row reads.
            src.execute("SET autocommit=0")
            lock_tables = all_tables if date_range else tables
            lock_list = ", ".join(f"{qname(schema, t['name'])} READ" for t in lock_tables)
            if lock_list:
                src.execute(f"LOCK TABLES {lock_list}")
                source_locked = True
        else:
            source.start_transaction(isolation_level="REPEATABLE READ", consistent_snapshot=True, readonly=True)
        range_keys: dict[str, set[tuple[Any, ...]]] | None = None
        if date_range:
            range_keys = _select_related_keys(src, schema, all_tables, date_range, progress_callback)
            selected_names = set(range_keys) - skipped_tables
            range_keys = {name: keys for name, keys in range_keys.items() if name not in skipped_tables}
            # Range resolution can reach tables copied by an earlier run. Keep
            # those tables available in the destination as foreign-key parents,
            # but do not clear or copy them again. During resume, retain an
            # existing table that is not checkpointed as complete so its partial
            # contents can be rebuilt from the saved range keys.
            tables = [
                table for table in all_tables
                if table["name"] in selected_names
                and (table["name"] not in existing_destination_tables
                     or (resume and table["name"] not in completed_names))
            ]
            selected_names = {table["name"] for table in tables}
            table_positions = {table["name"]: index for index, table in enumerate(tables, start=1)}
            range_external_references = [
                f"{table['name']}.{reference}"
                for table in tables
                for reference in table.get("external_references", [])
            ]
            if range_external_references:
                raise ValueError(
                    "Related rows have foreign keys to other schemas, which cannot be "
                    "safely redirected into the destination: " + "; ".join(range_external_references)
                )
            missing_completed = sorted(set(completed_counts) - existing_destination_tables)
            if missing_completed:
                raise ValueError("Resume checkpoint tables are missing from destination: " + ", ".join(missing_completed))
            collisions = sorted((selected_names & existing_destination_tables) - set(completed_counts)) if not resume else []
            if collisions and not date_range:
                raise ValueError("Destination already contains selected table(s): " + ", ".join(collisions)
                                 + ". Remove or rename them before copying")
            validate_rules(tables, rules)
        if mapping_db is not None:
            # Recreated tables must not retain mappings for source rows that were
            # deleted or rules that no longer apply. Completed tables in a resumed
            # copy are untouched; the interrupted table is rebuilt from row one.
            for table in tables:
                if table["name"] not in completed_names and not (
                    date_range and table["name"] in existing_destination_tables
                ):
                    mapping_db.execute(
                        "DELETE FROM mappings WHERE table_name=?", (table["name"],)
                    )
            mapping_db.commit()
        row_totals: dict[str, int] = {}
        for table in tables:
            if range_keys is not None and table["name"] not in set(date_range.get("full_tables", [])):
                row_totals[table["name"]] = len(range_keys.get(table["name"], set()))
            else:
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
                                   "total_rows": total_rows, "completed_rows": sum(counts.values()),
                                   "completed_tables": len(counts)})
            cols = [c for c in t["columns"] if "GENERATED" not in c["EXTRA"].upper()]
            names = [c["COLUMN_NAME"] for c in cols]
            qcols = ",".join(f"`{c.replace('`', '``')}`" for c in names)
            primary = [c["COLUMN_NAME"] for c in cols if c["COLUMN_KEY"] == "PRI"]
            full_table_copy = date_range is not None and name in set(date_range.get("full_tables", []))
            if range_keys is None or full_table_copy:
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
            def row_batches():
                if range_keys is None or full_table_copy:
                    while rows := src.fetchmany(batch_size):
                        yield rows
                    return
                if not primary:
                    raise ValueError(f"Relationship-preserving date-range copy requires a primary key on {name!r}")
                selected_keys = list(range_keys.get(name, set()))
                for offset in range(0, len(selected_keys), 400):
                    part = selected_keys[offset:offset + 400]
                    where = _key_predicate(primary, len(part))
                    params = [value for key in part for value in key] if len(primary) > 1 else [key[0] for key in part]
                    src.execute(f"SELECT {qcols} FROM {qname(schema, name)} WHERE {where}", tuple(params))
                    while rows := src.fetchmany(batch_size):
                        yield rows

            for rows in row_batches():
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
                        if (mapping_db is not None and rule.get("type") != "password"
                                and original_value is not None and new_value is not None):
                            original_text = str(original_value)
                            map_key = (col, original_text)
                            if map_key not in mapping_seen:
                                mapping_seen.add(map_key)
                                mapping_rows.append((name, col, original_text, str(new_value)))
                    output.append(tuple(values))
                try:
                    _executemany_with_packet_limit(dst, insert, output,
                                                   max_allowed_packet, len(names))
                except Exception as exc:
                    # Importing mysql.connector also loads its optional native
                    # extension. Keep that import lazy so the Qt process never
                    # loads it just by importing the application modules.
                    from mysql.connector import Error as ConnectorError
                    if not isinstance(exc, ConnectorError):
                        raise
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
                                       "completed_rows": sum(counts.values()) + count,
                                       "completed_tables": len(counts)})
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
