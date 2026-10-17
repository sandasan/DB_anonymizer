from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime, timedelta
from typing import Any

import mysql.connector


def load_json(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        obj = json.load(f)
    if not isinstance(obj, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return obj


def connect(settings: dict[str, Any]):
    options = dict(settings)
    env = options.pop("password_env", None)
    if env:
        if env not in os.environ:
            raise ValueError(f"Environment variable {env} is not set")
        options["password"] = os.environ[env]
    return mysql.connector.connect(**options)


def qname(schema: str, table: str) -> str:
    return f"`{schema.replace('`', '``')}`.`{table.replace('`', '``')}`"


def inspect_schema(conn, schema: str) -> list[dict[str, Any]]:
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE' ORDER BY TABLE_NAME", (schema,))
    tables = cur.fetchall()
    result = []
    for t in tables:
        cur.execute("SELECT COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, EXTRA FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION", (schema, t["TABLE_NAME"]))
        result.append({"name": t["TABLE_NAME"], "engine": t["ENGINE"], "columns": cur.fetchall()})
    cur.close()
    return result


def init_rules(tables: list[dict[str, Any]]) -> dict[str, Any]:
    rules: dict[str, Any] = {"confirm_copy_password_hashes": False, "tables": {}}
    for table in tables:
        preserve, anonymize = [], {}
        for c in table["columns"]:
            name, low = c["COLUMN_NAME"], c["COLUMN_NAME"].lower()
            if c["COLUMN_KEY"] == "PRI" or low.endswith("_id") or low == "id":
                preserve.append(name)
            if re.search(r"password|passwd|pwd|pass.?hash|login|username|user_name|email", low):
                preserve.append(name)
            elif re.search(r"phone|mobile|tel", low):
                anonymize[name] = {"type": "phone"}
            elif re.search(r"first.?name|last.?name|full.?name|^name$", low):
                anonymize[name] = {"type": "name"}
            elif re.search(r"address|street|city|postal|zip", low):
                anonymize[name] = {"type": "address"}
        rules["tables"][table["name"]] = {"preserve": sorted(set(preserve)), "anonymize": anonymize}
    return rules


def stable_number(table: str, column: str, value: Any, modulus: int) -> int:
    digest = hashlib.sha256(f"{table}\0{column}\0{value}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % modulus


def transform(value: Any, rule: dict[str, Any], table: str, column: str) -> Any:
    if value is None:
        return None
    kind = rule["type"]
    n = stable_number(table, column, value, 10**12)
    if kind == "constant":
        return rule["value"]
    if kind == "email":
        return f"user{stable_number(table, column, value, 10**14)}@example.invalid"
    if kind == "phone":
        return f"+1555{stable_number(table, column, value, 10**7):07d}"
    if kind == "name":
        return f"Person{n % 100000000:08d}"
    if kind == "unique":
        digest = hashlib.sha256(f"{table}\0{column}\0{value}".encode()).hexdigest()
        return f"anon{digest}"
    if kind == "address":
        return f"{n % 9999 + 1} Example Street"
    if kind == "date_shift":
        delta = timedelta(days=n % 7301 - 3650)
        if isinstance(value, (date, datetime)):
            return value + delta
        raise ValueError(f"date_shift configured on non-date {table}.{column}")
    raise ValueError(f"Unsupported transformation {kind!r} for {table}.{column}")


def unique_text(value: Any, candidate: Any, table: str, column: str, max_length: int,
                mapping: dict[Any, str], used: set[str]) -> str:
    """Fit an anonymized string to a unique text column and resolve collisions."""
    if value in mapping:
        return mapping[value]
    candidate = str(candidate)[:max_length]
    if candidate not in used:
        mapping[value] = candidate
        used.add(candidate)
        return candidate
    # A collision can occur after truncation or with a low-cardinality transform.
    # Allocate a deterministic hash pseudonym, retrying until it is unused.
    for attempt in range(100_000):
        digest = hashlib.sha256(f"{table}\0{column}\0{value}\0{attempt}".encode()).hexdigest()
        replacement = ("a" + digest)[:max_length]
        if replacement not in used:
            mapping[value] = replacement
            used.add(replacement)
            return replacement
    raise ValueError(f"Could not create a unique anonymized value for {table}.{column}; its maximum length may be too short")


def text_column_length(column_type: str) -> int:
    match = re.search(r"(?:var)?char\((\d+)\)", column_type, re.I)
    if match:
        return int(match.group(1))
    return 255


def validate_rules(tables: list[dict[str, Any]], rules: dict[str, Any]) -> None:
    existing = {t["name"]: t for t in tables}
    for tn, config in rules.get("tables", {}).items():
        if tn not in existing:
            raise ValueError(f"Rules reference missing table {tn!r}")
        cols = {c["COLUMN_NAME"]: c for c in existing[tn]["columns"]}
        preserved = config.get("preserve", [])
        for col in preserved:
            if col not in cols:
                raise ValueError(f"Rules reference missing column {tn}.{col}")
        for col, rule in config.get("anonymize", {}).items():
            if col not in cols:
                raise ValueError(f"Rules reference missing column {tn}.{col}")
            if col in preserved:
                raise ValueError(f"{tn}.{col} cannot be both preserved and anonymized")
            if re.search(r"password|passwd|pwd|pass.?hash|login|username|user_name|email", col, re.I):
                raise ValueError(f"Authentication column {tn}.{col} must be preserved so copied users can log in")
            if cols[col]["COLUMN_KEY"] == "UNI":
                if cols[col]["DATA_TYPE"].lower() not in {"char", "varchar", "tinytext", "text", "mediumtext", "longtext"}:
                    raise ValueError(f"Unique non-text column {tn}.{col} requires an application-specific transformation")
                if rule.get("type") not in {"email", "phone", "name", "address", "unique"}:
                    raise ValueError(f"Unique text column {tn}.{col} requires a text transformation such as name or unique")
            if rule.get("type") not in {"email", "phone", "name", "address", "date_shift", "constant", "unique"}:
                raise ValueError(f"Unsupported transform for {tn}.{col}")


def copy_database(source, destination, schema: str, rules: dict[str, Any], batch_size: int = 500,
                  allow_source_table_locks: bool = False) -> dict[str, int]:
    tables = inspect_schema(source, schema)
    non_transactional = [t["name"] for t in tables if t["engine"] != "InnoDB"]
    if non_transactional and not allow_source_table_locks:
        names = ", ".join(non_transactional)
        raise ValueError(f"Non-InnoDB source tables found ({names}). Set allow_source_table_locks=true only if it is acceptable to block writes to all source tables during the copy")
    validate_rules(tables, rules)
    dst, src = destination.cursor(), source.cursor()
    counts: dict[str, int] = {}
    source_locked = False
    try:
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
        dst.execute("SET FOREIGN_KEY_CHECKS=0")
        for t in tables:
            name = t["name"]
            src.execute(f"SHOW CREATE TABLE {qname(schema, name)}")
            ddl = src.fetchone()[1]
            quoted = name.replace("`", "``")
            ddl = re.sub(r"^CREATE TABLE `[^`]+`", f"CREATE TABLE `{quoted}`", ddl)
            dst.execute(ddl)
        for t in tables:
            name = t["name"]
            cols = [c for c in t["columns"] if "GENERATED" not in c["EXTRA"].upper()]
            names = [c["COLUMN_NAME"] for c in cols]
            qcols = ",".join(f"`{c.replace('`', '``')}`" for c in names)
            src.execute(f"SELECT {qcols} FROM {qname(schema, name)}")
            transforms = rules.get("tables", {}).get(name, {}).get("anonymize", {})
            column_info = {c["COLUMN_NAME"]: c for c in cols}
            unique_state = {
                col: ({}, set())
                for col in transforms
                if column_info[col]["COLUMN_KEY"] == "UNI"
            }
            insert = f"INSERT INTO `{name.replace('`', '``')}` ({qcols}) VALUES ({','.join(['%s'] * len(names))})"
            count = 0
            while rows := src.fetchmany(batch_size):
                output = []
                for row in rows:
                    values = list(row)
                    for i, col in enumerate(names):
                        if col in transforms:
                            new_value = transform(values[i], transforms[col], name, col)
                            if col in unique_state and new_value is not None:
                                mapping, used = unique_state[col]
                                new_value = unique_text(values[i], new_value, name, col,
                                                        text_column_length(column_info[col]["COLUMN_TYPE"]),
                                                        mapping, used)
                            values[i] = new_value
                    output.append(tuple(values))
                dst.executemany(insert, output)
                count += len(output)
            counts[name] = count
        destination.commit()
    except Exception:
        destination.rollback()
        raise
    finally:
        try:
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
        src.close()
        dst.close()
        if not source_locked:
            source.rollback()
    return counts
