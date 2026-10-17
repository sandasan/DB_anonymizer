from __future__ import annotations

import argparse
import json
import sys

from .core import connect, copy_database, init_rules, inspect_schema, load_json


def check_config(config: dict) -> None:
    for role in ("source", "destination"):
        if role not in config or not config[role].get("database"):
            raise ValueError(f"Config requires {role}.database")
    s, d = config["source"], config["destination"]
    if (s.get("host", "localhost").lower(), s.get("port", 3306), s["database"]) == (d.get("host", "localhost").lower(), d.get("port", 3306), d["database"]):
        raise ValueError("Source and destination must be different server/schema combinations")


def main() -> None:
    parser = argparse.ArgumentParser(prog="mysql-anonymizer")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("inspect", "init-config", "copy"):
        p = sub.add_parser(command)
        p.add_argument("--config", required=True)
        if command == "init-config":
            p.add_argument("--output", required=True)
        if command == "copy":
            p.add_argument("--rules", required=True)
    args = parser.parse_args()
    try:
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
                if not rules.get("confirm_copy_password_hashes"):
                    raise ValueError("Set confirm_copy_password_hashes=true in rules after reviewing the credential risk")
                if config.get("allow_source_table_locks"):
                    print("WARNING: allow_source_table_locks is enabled; if non-InnoDB tables exist, the copy will block writes to every source table until it finishes.", file=sys.stderr)
                destination = connect(config["destination"])
                try:
                    cur = destination.cursor()
                    cur.execute("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s", (config["destination"]["database"],))
                    if cur.fetchone()[0] != 0:
                        raise ValueError("Destination schema must be empty; nothing was changed")
                    cur.close()
                    counts = copy_database(source, destination, config["source"]["database"], rules,
                                           config.get("batch_size", 500), config.get("allow_source_table_locks", False))
                    print("Copy complete:")
                    for table, count in counts.items():
                        print(f"  {table}: {count} rows")
                finally:
                    destination.close()
        finally:
            source.close()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
