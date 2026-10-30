from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from .transformations import TEXT_DATA_TYPES

_TEMPORAL_DATA_TYPES = {"date", "datetime", "timestamp"}
_NUMERIC_DATA_TYPES = {"tinyint", "smallint", "mediumint", "int", "integer", "bigint", "decimal", "numeric", "float", "double", "real"}
_TEXT_TRANSFORMS = {"email", "url", "phone", "payment_key", "password", "card_mask", "card_number", "card_payexpirydate", "card_validitydate", "card_expirydate", "card_securitycode", "card_type", "description", "name", "entity_name", "address", "file_name", "ip_address", "unique", "passport", "pnr_code", "location_generalization"}
_SUPPORTED_TRANSFORMS = _TEXT_TRANSFORMS | {"date_shift", "date_round", "numeric_variance", "coordinate_round", "constant"}
_TRANSFORM_MAX_LENGTH = {
    # Keep generated emails within common VARCHAR(32) columns.
    "email": len("user") + 12 + len("@example.invalid"),
    "url": len("https://example.invalid/") + 24,
    "phone": len("+1555") + 7,
    "payment_key": len("test_") + 32,
    "password": 13,
    "card_mask": len("**** **** **** 0000"),
    "card_number": 16,
    "card_payexpirydate": 5,
    "card_validitydate": 10,
    "card_expirydate": 5,
    "card_securitycode": 3,
    "card_type": len("MASTERCARD"),
    "description": len("redacted"),
    "name": len("Person") + 8,
    "entity_name": len("Entity") + 8,
    "address": len("9999 Example Street"),
    "ip_address": 39,
    "unique": len("anon") + 64,
    "passport": 10,
    "pnr_code": 6,
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
            elif is_text and re.search(r"(?:^|_)(?:url|uri|website|homepage|web_url)(?:_|$)", low):
                anonymize[name] = {"type": "url"}
            elif is_text and re.search(r"phone|mobile|telephone|(^|_)tel(_|$)", hints):
                anonymize[name] = {"type": "phone"}
            elif is_text and ("ip" in tokens or "ipaddress" in tokens):
                anonymize[name] = {"type": "ip_address"}
            elif is_text and re.search(r"first.?name|last.?name|full.?name|given.?name|family.?name|surname|^name$", hints):
                anonymize[name] = {"type": "name"}
            elif is_text and re.search(r"(^|_)(address|street|city|postal|zip)(_|[0-9]|$)", low):
                anonymize[name] = {"type": "address"}
            elif data_type in _NUMERIC_DATA_TYPES and low in {"lat", "latitude", "lon", "lng", "long", "longitude"}:
                anonymize[name] = {"type": "coordinate_round", "precision": 2}
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
            auth_field = re.search(r"password|passwd|pwd|pass.?hash|login|username|user_name|token|secret|salt", col, re.I)
            payment_credential_field = (
                rule.get("type") == "payment_key"
                and re.search(r"key|token|secret", col, re.I)
                and not re.search(r"password|passwd|pwd|pass.?hash|login|username|user_name|salt", col, re.I)
            )
            password_transform = (
                rule.get("type") == "password"
                and re.search(r"password|passwd|pwd|pass.?hash", col, re.I)
            )
            if auth_field and not (payment_credential_field or password_transform):
                raise ValueError(f"Authentication column {tn}.{col} must be preserved so copied users can log in")
            if rule.get("type") == "password" and not password_transform:
                raise ValueError(f"password transformation requires a password or password-hash column: {tn}.{col}")
            if rule.get("type") in _TEXT_TRANSFORMS and cols[col]["DATA_TYPE"].lower() not in TEXT_DATA_TYPES:
                raise ValueError(
                    f"Text transformation {rule.get('type')!r} configured for non-text column "
                    f"{tn}.{col} ({cols[col]['COLUMN_TYPE']}); preserve it or choose a compatible rule"
                )
            kind = rule.get("type")
            data_type = cols[col]["DATA_TYPE"].lower()
            if kind in {"date_shift", "date_round"} and data_type not in _TEMPORAL_DATA_TYPES:
                raise ValueError(
                    f"{kind} configured for non-date column {tn}.{col} "
                    f"({cols[col]['COLUMN_TYPE']}); preserve it or choose a compatible rule"
                )
            if kind == "date_round" and rule.get("granularity", "month") not in {"month", "year", "decade"}:
                raise ValueError(f"Unsupported date_round granularity for {tn}.{col}")
            if kind == "numeric_variance":
                if data_type not in _NUMERIC_DATA_TYPES:
                    raise ValueError(f"numeric_variance configured for non-numeric column {tn}.{col} ({cols[col]['COLUMN_TYPE']})")
                try:
                    percent = float(rule.get("percent", 10))
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"numeric_variance percent must be between 0 and 100 for {tn}.{col}") from exc
                if not 0 <= percent <= 100:
                    raise ValueError(f"numeric_variance percent must be between 0 and 100 for {tn}.{col}")
            if kind == "coordinate_round":
                if data_type not in _NUMERIC_DATA_TYPES:
                    raise ValueError(f"coordinate_round configured for non-numeric column {tn}.{col} ({cols[col]['COLUMN_TYPE']})")
                if low not in {"lat", "latitude", "lon", "lng", "long", "longitude"}:
                    raise ValueError(f"coordinate_round requires a latitude or longitude column: {tn}.{col}")
                precision = rule.get("precision", 2)
                if isinstance(precision, bool) or not isinstance(precision, int) or not 0 <= precision <= 6:
                    raise ValueError(f"coordinate_round precision must be between 0 and 6 for {tn}.{col}")
            if kind == "location_generalization" and rule.get("target") != "hotel_class":
                raise ValueError(f"Unsupported location_generalization target for {tn}.{col}; use 'hotel_class'")
            if kind == "passport" and rule.get("country") not in {None, "GB", "gb"}:
                raise ValueError(f"Unsupported passport country format for {tn}.{col}; supported country is 'GB'")
            column_type = cols[col]["COLUMN_TYPE"]
            length_match = re.fullmatch(r"(?:var)?char\((\d+)\)", column_type, re.I)
            if length_match and (cols[col]["COLUMN_KEY"] != "UNI" or kind == "password"):
                max_length = int(length_match.group(1))
                # The copier shortens unique pseudonyms to the declared column
                # width and resolves collisions, including for narrow unique indexes.
                required_length = None if kind == "unique" else _TRANSFORM_MAX_LENGTH.get(kind)
                if kind == "constant" and isinstance(rule.get("value"), str):
                    required_length = len(rule["value"])
                if kind == "location_generalization" and isinstance(rule.get("value", "Hotel"), str):
                    required_length = len(rule.get("value", "Hotel"))
                if required_length is not None and required_length > max_length:
                    raise ValueError(
                        f"Transformation {kind!r} may produce {required_length} characters, "
                        f"but {tn}.{col} is limited to {max_length}; use a wider column or a shorter rule"
                    )
            if cols[col]["COLUMN_KEY"] == "UNI":
                if cols[col]["DATA_TYPE"].lower() not in TEXT_DATA_TYPES:
                    raise ValueError(f"Unique non-text column {tn}.{col} requires an application-specific transformation")
                if kind not in _TEXT_TRANSFORMS:
                    raise ValueError(f"Unique text column {tn}.{col} requires a text transformation such as name or unique")
            if kind not in _SUPPORTED_TRANSFORMS:
                raise ValueError(f"Unsupported transform for {tn}.{col}")
