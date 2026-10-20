"""Compatibility exports for the anonymizer application services."""

from .config import connect, load_json, save_json_atomic
from .copying import (CopyPaused, clear_destination_database_data, copy_database,
                      drop_destination_tables, recreate_destination_database)
from .mappings import list_mapping_fields, lookup_mapping, open_mapping_store
from .rules import init_rules, migrate_identifier_rules, prune_rules_to_schema, validate_rules
from .schema import inspect_schema, qname, resolve_table_selection
from .transformations import (
    TEXT_DATA_TYPES,
    stable_number,
    text_column_length,
    transform,
    unique_text,
)

__all__ = [
    "CopyPaused",
    "TEXT_DATA_TYPES",
    "connect",
    "copy_database",
    "drop_destination_tables",
    "clear_destination_database_data",
    "init_rules",
    "inspect_schema",
    "list_mapping_fields",
    "load_json",
    "lookup_mapping",
    "migrate_identifier_rules",
    "open_mapping_store",
    "prune_rules_to_schema",
    "qname",
    "resolve_table_selection",
    "recreate_destination_database",
    "save_json_atomic",
    "stable_number",
    "text_column_length",
    "transform",
    "unique_text",
    "validate_rules",
]
