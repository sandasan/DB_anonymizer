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
            "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, EXTRA "
            "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s "
            "ORDER BY TABLE_NAME, ORDINAL_POSITION",
            (schema,),
        )
        columns_by_table: dict[str, list[dict[str, Any]]] = {}
        for column in cur.fetchall():
            columns_by_table.setdefault(column["TABLE_NAME"], []).append({
                key: value for key, value in column.items() if key != "TABLE_NAME"
            })

        cur.execute(
            "SELECT DISTINCT TABLE_NAME, CONSTRAINT_NAME, REFERENCED_TABLE_SCHEMA, REFERENCED_TABLE_NAME "
            "FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=%s "
            "AND REFERENCED_TABLE_NAME IS NOT NULL",
            (schema,),
        )
        references_by_table: dict[str, list[str]] = {}
        external_references_by_table: dict[str, list[str]] = {}
        for reference in cur.fetchall():
            if reference["REFERENCED_TABLE_SCHEMA"] == schema:
                references_by_table.setdefault(reference["TABLE_NAME"], []).append(
                    reference["REFERENCED_TABLE_NAME"]
                )
            else:
                external_references_by_table.setdefault(reference["TABLE_NAME"], []).append(
                    f"{reference['CONSTRAINT_NAME']} -> "
                    f"{reference['REFERENCED_TABLE_SCHEMA']}.{reference['REFERENCED_TABLE_NAME']}"
                )

        return [{
            "name": table["TABLE_NAME"],
            "engine": table["ENGINE"],
            "columns": columns_by_table.get(table["TABLE_NAME"], []),
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
