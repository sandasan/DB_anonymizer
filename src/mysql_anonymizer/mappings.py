from __future__ import annotations

import os
import sqlite3
import threading


def open_mapping_store(path: str) -> sqlite3.Connection:
    """Open a private SQLite index of reversible anonymization mappings."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if not os.path.exists(path):
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    os.chmod(path, 0o600)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS mappings (
        table_name TEXT NOT NULL, column_name TEXT NOT NULL,
        original_value TEXT NOT NULL, anonymized_value TEXT NOT NULL,
        PRIMARY KEY (table_name, column_name, original_value)
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS mappings_reverse ON mappings(table_name, column_name, anonymized_value)")
    return db


def lookup_mapping(path: str, table: str, column: str, value: str,
                   direction: str = "original-to-anonymized",
                   cancel_event: threading.Event | None = None,
                   match_mode: str = "exact") -> list[str]:
    if direction == "original-to-anonymized":
        source_column, result_column = "original_value", "anonymized_value"
    elif direction == "anonymized-to-original":
        source_column, result_column = "anonymized_value", "original_value"
    else:
        raise ValueError(f"Unsupported lookup direction {direction!r}")
    if match_mode == "exact":
        condition = f"{source_column}=?"
    elif match_mode == "contains":
        condition = f"instr(casefold({source_column}), casefold(?)) > 0"
    else:
        raise ValueError(f"Unsupported lookup match mode {match_mode!r}")
    sql = (f"SELECT DISTINCT {result_column} FROM mappings WHERE table_name=? "
           f"AND column_name=? AND {condition} ORDER BY {result_column}")
    params = (table, column, value)
    with sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True) as db:
        if match_mode == "contains":
            db.create_function("casefold", 1, lambda item: item.casefold() if item is not None else None)
        if cancel_event is not None:
            db.set_progress_handler(lambda: int(cancel_event.is_set()), 100)
        return [row[0] for row in db.execute(sql, params)]


def lookup_original_value(path: str, value: str) -> list[tuple[str, str, str]]:
    """Find original values for an anonymized value across all mapped fields."""
    with sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True) as db:
        return db.execute(
            "SELECT DISTINCT table_name, column_name, original_value FROM mappings "
            "WHERE anonymized_value=? ORDER BY table_name, column_name, original_value",
            (value,),
        ).fetchall()


def lookup_value_all_fields(path: str, value: str, direction: str,
                            cancel_event: threading.Event | None = None,
                            match_mode: str = "exact") -> list[tuple[str, str, str]]:
    """Find values across all mapped fields, returning table, column, and match."""
    if direction == "original-to-anonymized":
        match_column, result_column = "original_value", "anonymized_value"
    elif direction == "anonymized-to-original":
        match_column, result_column = "anonymized_value", "original_value"
    else:
        raise ValueError(f"Unsupported lookup direction {direction!r}")
    if match_mode == "exact":
        condition = f"{match_column}=?"
    elif match_mode == "contains":
        condition = f"instr(casefold({match_column}), casefold(?)) > 0"
    else:
        raise ValueError(f"Unsupported lookup match mode {match_mode!r}")
    with sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True) as db:
        if match_mode == "contains":
            db.create_function("casefold", 1, lambda item: item.casefold() if item is not None else None)
        if cancel_event is not None:
            db.set_progress_handler(lambda: int(cancel_event.is_set()), 100)
        return db.execute(
            f"SELECT DISTINCT table_name, column_name, {result_column} FROM mappings "
            f"WHERE {condition} ORDER BY table_name, column_name, {result_column}",
            (value,),
        ).fetchall()


def list_mapping_fields(path: str) -> dict[str, list[str]]:
    """Return the tables and columns recorded in an existing mapping store."""
    fields: dict[str, list[str]] = {}
    with sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True) as db:
        for table, column in db.execute(
            "SELECT DISTINCT table_name, column_name FROM mappings ORDER BY table_name, column_name"
        ):
            fields.setdefault(table, []).append(column)
    return fields


def clear_mapping_store(path: str) -> int:
    """Delete every saved mapping and checkpoint the SQLite store."""
    if not os.path.exists(path):
        return 0
    db = open_mapping_store(path)
    try:
        cursor = db.execute("DELETE FROM mappings")
        deleted = cursor.rowcount
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("VACUUM")
        return deleted
    finally:
        db.close()
