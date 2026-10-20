from __future__ import annotations

import argparse
import json
import sys

from .core import connect, copy_database, init_rules, inspect_schema, load_json, lookup_mapping, migrate_identifier_rules, save_json_atomic


def check_config(config: dict) -> None:
    for role in ("source", "destination"):
        if role not in config or not config[role].get("database"):
            raise ValueError(f"Config requires {role}.database")
    s, d = config["source"], config["destination"]
    def identity(settings: dict) -> tuple[str, int, str]:
        return (
            str(settings.get("host", "localhost")).casefold(),
            int(settings.get("port", 3306)),
            str(settings["database"]).casefold(),
        )

    if identity(s) == identity(d):
        raise ValueError("Source and destination must be different server/schema combinations")


def main() -> None:
    parser = argparse.ArgumentParser(prog="mysql-anonymizer")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("inspect", "init-config", "copy", "lookup"):
        p = sub.add_parser(command)
        if command != "lookup":
            p.add_argument("--config", required=True)
        if command == "init-config":
            p.add_argument("--output", required=True)
        if command == "copy":
            p.add_argument("--rules", required=True)
            p.add_argument("--mapping-store", help="SQLite file for bidirectional value lookups")
            p.add_argument("--table", action="append", dest="selected_tables",
                           help="Table to copy; repeat to select multiple tables (default: all)")
            p.add_argument("--include-dependencies", action="store_true",
                           help="Automatically include referenced tables, recursively")
        if command == "lookup":
            p.add_argument("--mapping-store", required=True)
            p.add_argument("--table", required=True)
            p.add_argument("--column", required=True)
            p.add_argument("--value", required=True)
            p.add_argument("--direction", choices=("original-to-anonymized", "anonymized-to-original"), required=True)
    args = parser.parse_args()
    try:
        if args.command == "lookup":
            results = lookup_mapping(args.mapping_store, args.table, args.column, args.value, args.direction)
            if results:
                for result in results:
                    print(result)
            else:
                print("No matching mapping found", file=sys.stderr)
                raise SystemExit(2)
            return
        config = load_json(args.config)
        check_config(config)
        source = connect(config["source"])
        try:
            tables = inspect_schema(source, config["source"]["database"])
            if args.command == "inspect":
                for table in tables:
                    print(f"{table['name']} ({table['engine']})")
                    for c in table["columns"]:
                        print(f"  {c['COLUMN_NAME']}: {c['COLUMN_TYPE']} {c['COLUMN_KEY']}")
            elif args.command == "init-config":
                with open(args.output, "w", encoding="utf-8") as f:
                    json.dump(init_rules(tables), f, indent=2)
                    f.write("\n")
                print(f"Wrote starter rules to {args.output}; review every table and column.")
            else:
                rules = load_json(args.rules)
                moved = migrate_identifier_rules(rules, tables)
                if moved:
                    save_json_atomic(args.rules, rules)
                    print("Automatically moved identifier columns to preserve in rules.json: " + ", ".join(moved), file=sys.stderr)
                if not rules.get("confirm_copy_password_hashes"):
                    raise ValueError("Set confirm_copy_password_hashes=true in rules after reviewing the credential risk")
                if config.get("allow_source_table_locks"):
                    print("WARNING: allow_source_table_locks is enabled; if non-InnoDB tables exist, the copy will block writes to every source table until it finishes.", file=sys.stderr)
                destination = connect(config["destination"])
                try:
                    counts = copy_database(source, destination, config["source"]["database"], rules,
                                           config.get("batch_size", 2000), config.get("allow_source_table_locks", False),
                                           args.mapping_store, args.selected_tables, args.include_dependencies)
                    copied = set(counts)
                    print("Copy complete. Copied tables:")
                    for table, count in counts.items():
                        print(f"  {table}: {count} rows")
                    not_copied = [table["name"] for table in tables if table["name"] not in copied]
                    print("Not copied tables:")
                    if not_copied:
                        for table in not_copied:
                            print(f"  {table}")
                    else:
                        print("  (none)")
                finally:
                    destination.close()
        finally:
            source.close()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
