from __future__ import annotations

from typing import Any


def qname(schema: str, table: str) -> str:
    return f"`{schema.replace('`', '``')}`.`{table.replace('`', '``')}`"


def inspect_schema(conn, schema: str) -> list[dict[str, Any]]:
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE' ORDER BY TABLE_NAME",
            (schema,),
        )
        tables = cur.fetchall()
        if not tables:
            return []

        cur.execute(
            "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, EXTRA, COLUMN_COMMENT "
            "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s "
            "ORDER BY TABLE_NAME, ORDINAL_POSITION",
            (schema,),
        )
        columns_by_table: dict[str, list[dict[str, Any]]] = {}
        for column in cur.fetchall():
            columns_by_table.setdefault(column["TABLE_NAME"], []).append({
                key: value for key, value in column.items() if key != "TABLE_NAME"
            })

        # COLUMN_KEY only reports UNI for a single-column unique index. Columns
        # participating in a composite unique index need the same collision
        # protection during anonymization, so record all unique-index members.
        cur.execute(
            "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA=%s AND NON_UNIQUE=0",
            (schema,),
        )
        unique_columns: dict[str, set[str]] = {}
        for index_column in cur.fetchall():
            unique_columns.setdefault(index_column["TABLE_NAME"], set()).add(
                index_column["COLUMN_NAME"]
            )
        for table_name, columns in columns_by_table.items():
            unique = unique_columns.get(table_name, set())
            for column in columns:
                column["IS_UNIQUE"] = (
                    column["COLUMN_NAME"] in unique or column["COLUMN_KEY"] == "UNI"
                )

        cur.execute(
            "SELECT TABLE_NAME, COLUMN_NAME, CONSTRAINT_NAME, REFERENCED_TABLE_SCHEMA, "
            "REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME, ORDINAL_POSITION "
            "FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=%s "
            "AND REFERENCED_TABLE_NAME IS NOT NULL",
            (schema,),
        )
        references_by_table: dict[str, list[str]] = {}
        foreign_key_columns: dict[str, set[str]] = {}
        foreign_keys: dict[str, dict[str, dict[str, Any]]] = {}
        external_references_by_table: dict[str, list[str]] = {}
        for reference in cur.fetchall():
            foreign_key_columns.setdefault(reference["TABLE_NAME"], set()).add(reference["COLUMN_NAME"])
            if reference["REFERENCED_TABLE_SCHEMA"] == schema:
                references_by_table.setdefault(reference["TABLE_NAME"], []).append(
                    reference["REFERENCED_TABLE_NAME"]
                )
                key = foreign_keys.setdefault(reference["TABLE_NAME"], {}).setdefault(
                    reference["CONSTRAINT_NAME"],
                    {"name": reference["CONSTRAINT_NAME"],
                     "referenced_table": reference["REFERENCED_TABLE_NAME"],
                     "columns": []},
                )
                key["columns"].append((
                    reference["ORDINAL_POSITION"], reference["COLUMN_NAME"],
                    reference["REFERENCED_COLUMN_NAME"],
                ))
            else:
                external_references_by_table.setdefault(reference["TABLE_NAME"], []).append(
                    f"{reference['CONSTRAINT_NAME']} -> "
                    f"{reference['REFERENCED_TABLE_SCHEMA']}.{reference['REFERENCED_TABLE_NAME']}"
                )

        for table_keys in foreign_keys.values():
            for key in table_keys.values():
                key["columns"].sort()
                key["columns"] = [
                    {"column": child, "referenced_column": parent}
                    for _, child, parent in key["columns"]
                ]

        return [{
            "name": table["TABLE_NAME"],
            "engine": table["ENGINE"],
            "columns": columns_by_table.get(table["TABLE_NAME"], []),
            "foreign_key_columns": sorted(foreign_key_columns.get(table["TABLE_NAME"], set())),
            "foreign_keys": list(foreign_keys.get(table["TABLE_NAME"], {}).values()),
            "references": references_by_table.get(table["TABLE_NAME"], []),
            "external_references": external_references_by_table.get(table["TABLE_NAME"], []),
        } for table in tables]
    finally:
        cur.close()


def resolve_table_selection(tables: list[dict[str, Any]], selected_tables: list[str] | None,
                            include_dependencies: bool = False,
                            existing_destination_tables: set[str] | None = None) -> list[str]:
    available = {table["name"] for table in tables}
    if selected_tables is None:
        return [table["name"] for table in tables]
    if not selected_tables:
        raise ValueError("Select at least one table to copy")
    unknown = sorted(set(selected_tables) - available)
    if unknown:
        raise ValueError("Unknown source table(s): " + ", ".join(unknown))

    existing_destination_tables = existing_destination_tables or set()
    selected = set(selected_tables)
    if include_dependencies:
        by_name = {table["name"]: table for table in tables}
        pending = list(selected)
        while pending:
            current = pending.pop()
            for dependency in by_name[current].get("references", []):
                if dependency not in selected and dependency not in existing_destination_tables:
                    selected.add(dependency)
                    pending.append(dependency)
    else:
        missing = sorted({ref for table in tables if table["name"] in selected
                          for ref in table.get("references", [])
                          if ref not in selected and ref not in existing_destination_tables})
        if missing:
            raise ValueError("Selected tables reference tables that are not selected: "
                             + ", ".join(missing) + ". Include these dependencies with --table or use --include-dependencies")
    return [table["name"] for table in tables if table["name"] in selected]


def expand_related_tables(tables: list[dict[str, Any]], selected_tables: list[str]) -> list[str]:
    """Expand selections to the full connected component of same-schema FKs."""
    by_name = {table["name"]: table for table in tables}
    unknown = sorted(set(selected_tables) - by_name.keys())
    if unknown:
        raise ValueError("Unknown source table(s): " + ", ".join(unknown))
    related = set(selected_tables)
    pending = list(selected_tables)
    while pending:
        current = pending.pop()
        neighbors = set(by_name[current].get("references", []))
        neighbors.update(
            table["name"] for table in tables
            if current in table.get("references", [])
        )
        for neighbor in neighbors - related:
            related.add(neighbor)
            pending.append(neighbor)
    return [table["name"] for table in tables if table["name"] in related]
