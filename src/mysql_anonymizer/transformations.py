from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta
from typing import Any

TEXT_DATA_TYPES = {"char", "varchar", "tinytext", "text", "mediumtext", "longtext"}


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
