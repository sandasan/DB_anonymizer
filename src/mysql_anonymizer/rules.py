from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from .transformations import TEXT_DATA_TYPES


def migrate_identifier_rules(rules: dict[str, Any], tables: list[dict[str, Any]]) -> list[str]:
    """Move primary and conventional ID columns out of anonymization rules."""
    primary_keys = {
        table["name"]: {c["COLUMN_NAME"] for c in table["columns"] if c["COLUMN_KEY"] == "PRI"}
        for table in tables
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
            if column in identifiers or low == "id" or low.endswith("_id"):
                del anonymize[column]
                if column not in preserve:
                    preserve.append(column)
                moved.append(f"{table}.{column}")
        config["preserve"] = sorted(set(preserve))
    return moved


def init_rules(tables: list[dict[str, Any]]) -> dict[str, Any]:
    rules: dict[str, Any] = {"confirm_copy_password_hashes": False, "tables": {}}
    for table in tables:
        preserve, anonymize = [], {}
        for c in table["columns"]:
            name, low = c["COLUMN_NAME"], c["COLUMN_NAME"].lower()
            is_text = c["DATA_TYPE"].lower() in TEXT_DATA_TYPES
            is_identifier = c["COLUMN_KEY"] == "PRI" or low.endswith("_id") or low == "id"
            if is_identifier:
                preserve.append(name)
            if re.search(r"password|passwd|pwd|pass.?hash|login|username|user_name|email", low):
                preserve.append(name)
            elif is_identifier:
                # Keep keys and foreign-key columns unchanged to retain relationships.
                continue
            elif is_text and re.search(r"phone|mobile|telephone|(^|_)tel(_|$)", low):
                anonymize[name] = {"type": "phone"}
            elif is_text and re.search(r"first.?name|last.?name|full.?name|^name$", low):
                anonymize[name] = {"type": "name"}
            elif is_text and re.search(r"address|street|city|postal|zip", low):
                anonymize[name] = {"type": "address"}
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
            if re.search(r"password|passwd|pwd|pass.?hash|login|username|user_name|email", col, re.I):
                raise ValueError(f"Authentication column {tn}.{col} must be preserved so copied users can log in")
            if rule.get("type") in {"email", "phone", "name", "address", "unique"} and cols[col]["DATA_TYPE"].lower() not in TEXT_DATA_TYPES:
                raise ValueError(
                    f"Text transformation {rule.get('type')!r} configured for non-text column "
                    f"{tn}.{col} ({cols[col]['COLUMN_TYPE']}); preserve it or choose a compatible rule"
                )
            if cols[col]["COLUMN_KEY"] == "UNI":
                if cols[col]["DATA_TYPE"].lower() not in TEXT_DATA_TYPES:
                    raise ValueError(f"Unique non-text column {tn}.{col} requires an application-specific transformation")
                if rule.get("type") not in {"email", "phone", "name", "address", "unique"}:
                    raise ValueError(f"Unique text column {tn}.{col} requires a text transformation such as name or unique")
            if rule.get("type") not in {"email", "phone", "name", "address", "date_shift", "constant", "unique"}:
                raise ValueError(f"Unsupported transform for {tn}.{col}")
