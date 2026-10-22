from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from .transformations import TEXT_DATA_TYPES

_TEMPORAL_DATA_TYPES = {"date", "datetime", "timestamp"}
_TRANSFORM_MAX_LENGTH = {
    # Keep generated emails within common VARCHAR(32) columns.
    "email": len("user") + 12 + len("@example.invalid"),
    "phone": len("+1555") + 7,
    "name": len("Person") + 8,
    "address": len("9999 Example Street"),
    "ip_address": 39,
    "unique": len("anon") + 64,
}


def migrate_identifier_rules(rules: dict[str, Any], tables: list[dict[str, Any]]) -> list[str]:
    """Move primary and conventional ID columns out of anonymization rules."""
    primary_keys = {
        table["name"]: {c["COLUMN_NAME"] for c in table["columns"] if c["COLUMN_KEY"] == "PRI"}
        for table in tables
    }
    foreign_keys = {
        table["name"]: set(table.get("foreign_key_columns", [])) for table in tables
    }
    moved = []
    for table, config in rules.get("tables", {}).items():
        anonymize = config.get("anonymize", {})
        preserve = config.setdefault("preserve", [])
        if not isinstance(anonymize, dict) or not isinstance(preserve, list):
            raise ValueError(f"Invalid preserve/anonymize rules for table {table!r}")
        identifiers = primary_keys.get(table, set())
        for column in list(anonymize):
            low = column.lower()
            if column in identifiers or column in foreign_keys.get(table, set()) or low == "id" or low.endswith("_id"):
                del anonymize[column]
                if column not in preserve:
                    preserve.append(column)
                moved.append(f"{table}.{column}")
        config["preserve"] = sorted(set(preserve))
    return moved


def init_rules(tables: list[dict[str, Any]]) -> dict[str, Any]:
    """Generate conservative rules from inspected schema metadata."""
    rules: dict[str, Any] = {"confirm_copy_password_hashes": False, "tables": {}}
    for table in tables:
        preserve, anonymize = [], {}
        foreign_keys = set(table.get("foreign_key_columns", []))
        for c in table["columns"]:
            name, low = c["COLUMN_NAME"], c["COLUMN_NAME"].lower()
            data_type = c["DATA_TYPE"].lower()
            is_text = data_type in TEXT_DATA_TYPES
            is_identifier = c["COLUMN_KEY"] == "PRI" or name in foreign_keys or low.endswith("_id") or low == "id"
            table_name = table["name"].lower()
            comment = str(c.get("COLUMN_COMMENT") or "").lower()
            tokens = set(re.findall(r"[a-z0-9]+", low))
            hints = f"{low} {comment}"
            non_personal = bool(tokens & {"type", "status", "kind", "category", "code", "format", "source"})
            auth_table = bool(re.search(r"(^|_)(user|users|account|accounts|auth|login)(_|$)", table_name))
            if is_identifier:
                preserve.append(name)
            if re.search(r"password|passwd|pwd|pass.?hash|login|username|user_name|token|secret|salt", low):
                preserve.append(name)
            elif is_identifier or non_personal:
                continue
            elif is_text and re.search(r"email|e_mail", hints):
                # Email may be a login identifier in account tables; retain it there.
                if auth_table or re.search(r"primary|login|auth", comment):
                    preserve.append(name)
                else:
                    anonymize[name] = {"type": "email"}
            elif is_text and re.search(r"phone|mobile|telephone|(^|_)tel(_|$)", hints):
                anonymize[name] = {"type": "phone"}
            elif is_text and ("ip" in tokens or "ipaddress" in tokens):
                anonymize[name] = {"type": "ip_address"}
            elif is_text and re.search(r"first.?name|last.?name|full.?name|given.?name|family.?name|surname|^name$", hints):
                anonymize[name] = {"type": "name"}
            elif is_text and re.search(r"(^|_)(address|street|city|postal|zip)(_|[0-9]|$)", low):
                anonymize[name] = {"type": "address"}
            elif data_type in _TEMPORAL_DATA_TYPES and re.search(r"birth|dob|date_of_birth", hints):
                anonymize[name] = {"type": "date_shift"}
        rules["tables"][table["name"]] = {"preserve": sorted(set(preserve)), "anonymize": anonymize}
    return rules


def prune_rules_to_schema(rules: dict[str, Any], tables: list[dict[str, Any]],
                          target_tables: set[str]) -> tuple[dict[str, Any], list[str]]:
    """Copy rules while dropping stale column references for tables being copied."""
    compatible = deepcopy(rules)
    source_columns = {
        table["name"]: {column["COLUMN_NAME"] for column in table["columns"]}
        for table in tables if table["name"] in target_tables
    }
    removed: list[str] = []
    table_rules = compatible.get("tables", {})
    if not isinstance(table_rules, dict):
        return compatible, removed
    for table_name, available_columns in source_columns.items():
        config = table_rules.get(table_name)
        if not isinstance(config, dict):
            continue
        preserve = config.get("preserve", [])
        if isinstance(preserve, list):
            for column in preserve[:]:
                if column not in available_columns:
                    preserve.remove(column)
                    removed.append(f"{table_name}.{column}")
        anonymize = config.get("anonymize", {})
        if isinstance(anonymize, dict):
            for column in list(anonymize):
                if column not in available_columns:
                    del anonymize[column]
                    removed.append(f"{table_name}.{column}")
    return compatible, sorted(set(removed))


def validate_rules(tables: list[dict[str, Any]], rules: dict[str, Any]) -> None:
    existing = {t["name"]: t for t in tables}
    for tn, config in rules.get("tables", {}).items():
        if tn not in existing:
            continue
        cols = {c["COLUMN_NAME"]: c for c in existing[tn]["columns"]}
        preserved = config.get("preserve", [])
        for col in preserved:
            if col not in cols:
                raise ValueError(f"Rules reference missing column {tn}.{col}")
        for col, rule in config.get("anonymize", {}).items():
            if col not in cols:
                raise ValueError(f"Rules reference missing column {tn}.{col}")
            low = col.lower()
            if cols[col]["COLUMN_KEY"] == "PRI" or low == "id" or low.endswith("_id"):
                raise ValueError(f"Identifier column {tn}.{col} must be preserved to keep database relationships intact; remove it from anonymize")
            if col in preserved:
                raise ValueError(f"{tn}.{col} is in both preserve and anonymize; remove it from anonymize")
            if re.search(r"password|passwd|pwd|pass.?hash|login|username|user_name|token|secret|salt", col, re.I):
                raise ValueError(f"Authentication column {tn}.{col} must be preserved so copied users can log in")
            if rule.get("type") in {"email", "phone", "name", "address", "ip_address", "unique"} and cols[col]["DATA_TYPE"].lower() not in TEXT_DATA_TYPES:
                raise ValueError(
                    f"Text transformation {rule.get('type')!r} configured for non-text column "
                    f"{tn}.{col} ({cols[col]['COLUMN_TYPE']}); preserve it or choose a compatible rule"
                )
            kind = rule.get("type")
            data_type = cols[col]["DATA_TYPE"].lower()
            if kind == "date_shift" and data_type not in _TEMPORAL_DATA_TYPES:
                raise ValueError(
                    f"date_shift configured for non-date column {tn}.{col} "
                    f"({cols[col]['COLUMN_TYPE']}); preserve it or choose a compatible rule"
                )
            column_type = cols[col]["COLUMN_TYPE"]
            length_match = re.fullmatch(r"(?:var)?char\((\d+)\)", column_type, re.I)
            if length_match and cols[col]["COLUMN_KEY"] != "UNI":
                max_length = int(length_match.group(1))
                # The copier shortens unique pseudonyms to the declared column
                # width and resolves collisions, including for narrow unique indexes.
                required_length = None if kind == "unique" else _TRANSFORM_MAX_LENGTH.get(kind)
                if kind == "constant" and isinstance(rule.get("value"), str):
                    required_length = len(rule["value"])
                if required_length is not None and required_length > max_length:
                    raise ValueError(
                        f"Transformation {kind!r} may produce {required_length} characters, "
                        f"but {tn}.{col} is limited to {max_length}; use a wider column or a shorter rule"
                    )
            if cols[col]["COLUMN_KEY"] == "UNI":
                if cols[col]["DATA_TYPE"].lower() not in TEXT_DATA_TYPES:
                    raise ValueError(f"Unique non-text column {tn}.{col} requires an application-specific transformation")
                if rule.get("type") not in {"email", "phone", "name", "address", "ip_address", "unique"}:
                    raise ValueError(f"Unique text column {tn}.{col} requires a text transformation such as name or unique")
            if rule.get("type") not in {"email", "phone", "name", "address", "ip_address", "date_shift", "constant", "unique"}:
                raise ValueError(f"Unsupported transform for {tn}.{col}")
