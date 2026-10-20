from __future__ import annotations

import os
import sqlite3


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
                   direction: str = "original-to-anonymized") -> list[str]:
    if direction == "original-to-anonymized":
        sql, params = "SELECT DISTINCT anonymized_value FROM mappings WHERE table_name=? AND column_name=? AND original_value=? ORDER BY anonymized_value", (table, column, value)
    elif direction == "anonymized-to-original":
        sql, params = "SELECT DISTINCT original_value FROM mappings WHERE table_name=? AND column_name=? AND anonymized_value=? ORDER BY original_value", (table, column, value)
    else:
        raise ValueError(f"Unsupported lookup direction {direction!r}")
    with sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True) as db:
        return [row[0] for row in db.execute(sql, params)]


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
