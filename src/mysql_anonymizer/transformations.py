from __future__ import annotations

import hashlib
import ipaddress
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
    if kind == "constant":
        return rule["value"]
    if kind == "email":
        return f"user{stable_number(table, column, value, 10**12)}@example.invalid"
    if kind == "phone":
        return f"+1555{stable_number(table, column, value, 10**7):07d}"
    if kind == "unique":
        digest = hashlib.sha256(f"{table}\0{column}\0{value}".encode()).hexdigest()
        return f"anon{digest}"
    if kind == "ip_address":
        try:
            address = ipaddress.ip_address(str(value).strip())
        except ValueError as exc:
            raise ValueError(f"Invalid IP address in {table}.{column}") from exc
        digest = hashlib.sha256(f"{table}\0{column}\0{value}".encode()).digest()
        if address.version == 4:
            replacement = ipaddress.IPv4Address(bytes((10, *digest[:3])))
        else:
            replacement = ipaddress.IPv6Address(bytes.fromhex("20010db8") + digest[:12])
        return str(replacement)
    n = stable_number(table, column, value, 10**12)
    if kind == "name":
        return f"Person{n % 100000000:08d}"
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
    candidate = str(candidate)
    if 0 < len(candidate) <= max_length and candidate not in used:
        mapping[value] = candidate
        used.add(candidate)
        return candidate
    if max_length < 1:
        raise ValueError(f"Cannot create a unique anonymized value for {table}.{column} with zero available characters")

    # A collision can occur after truncation or with a low-cardinality transform.
    # Encode a deterministic digest in a compact alphabet so even short columns
    # can hold distinct pseudonyms when their value space permits it.
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    capacity = len(alphabet) ** max_length
    if len(used) >= capacity:
        raise ValueError(f"Could not create a unique anonymized value for {table}.{column}; its maximum length may be too short")

    for attempt in range(min(capacity, 100_000)):
        raw = hashlib.sha256(f"{table}\0{column}\0{value}\0{attempt}".encode()).digest()
        number = int.from_bytes(raw, "big") % capacity
        chars = []
        for _ in range(max_length):
            number, digit = divmod(number, len(alphabet))
            chars.append(alphabet[digit])
        replacement = "".join(reversed(chars))
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
