from __future__ import annotations

import json
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from .cli import check_config
from .core import (
    connect,
    copy_database,
    init_rules,
    inspect_schema,
    load_json,
    migrate_identifier_rules,
    save_json_atomic,
)


class AnonymizerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("MySQL Anonymized Copy")
        self.geometry("980x760")
        self.minsize(800, 620)
        self.tables: list[dict[str, Any]] = []
        self.table_names: list[str] = []
        self.messages: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.busy = False

        self.config_path = tk.StringVar(value="config.json")
        self.rules_path = tk.StringVar(value="rules.json")
        self.mapping_path = tk.StringVar()
        self.allow_locks = tk.BooleanVar(value=False)
        self.include_dependencies = tk.BooleanVar(value=False)
        self.connection_fields: dict[str, dict[str, tk.StringVar]] = {}
        self.password_fields: dict[str, tk.StringVar] = {}
        self._build_ui()
        self.after(100, self._poll_messages)

    def _build_ui(self) -> None:
        paths = ttk.Frame(self, padding=(12, 12, 12, 4))
        paths.pack(fill="x")
        self._path_row(paths, 0, "Config", self.config_path, self._choose_config)
        self._path_row(paths, 1, "Rules", self.rules_path, self._choose_rules)
        self._path_row(paths, 2, "Mapping store (optional)", self.mapping_path, self._choose_mapping)

        self.tabs_widget = ttk.Notebook(self)
        self.tabs_widget.pack(fill="both", expand=True, padx=12, pady=8)
        self.connection_tab = ttk.Frame(self.tabs_widget, padding=16)
        self.tables_tab = ttk.Frame(self.tabs_widget, padding=12)
        self.rules_tab = ttk.Frame(self.tabs_widget, padding=12)
        self.tabs_widget.add(self.connection_tab, text="Connections")
        self.tabs_widget.add(self.tables_tab, text="Tables")
        self.tabs_widget.add(self.rules_tab, text="Anonymization rules")
        self._build_connections()
        self._build_tables()
        self._build_rules()

        footer = ttk.Frame(self, padding=(12, 4, 12, 12))
        footer.pack(fill="both")
        buttons = ttk.Frame(footer)
        buttons.pack(fill="x", pady=(0, 8))
        self.save_config_button = ttk.Button(buttons, text="Save config", command=self._save_config)
        self.save_config_button.pack(side="left")
        self.inspect_button = ttk.Button(buttons, text="Inspect source", command=self._inspect)
        self.inspect_button.pack(side="left", padx=8)
        self.copy_button = ttk.Button(buttons, text="Copy selected tables", command=self._copy)
        self.copy_button.pack(side="right")
        self.status = tk.StringVar(value="Load or enter connection settings, then inspect the source.")
        ttk.Label(footer, textvariable=self.status).pack(anchor="w", pady=(0, 4))
        self.output = tk.Text(footer, height=8, wrap="word", state="disabled")
        self.output.pack(fill="both", expand=True)

    def _path_row(self, parent: ttk.Frame, row: int, label: str, var: tk.StringVar, browse) -> None:
        ttk.Label(parent, text=label, width=22).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew", padx=8, pady=2)
        ttk.Button(parent, text="Browse…", command=browse).grid(row=row, column=2, pady=2)
        parent.columnconfigure(1, weight=1)

    def _build_connections(self) -> None:
        for col, role in enumerate(("source", "destination")):
            frame = ttk.LabelFrame(self.connection_tab, text=role.title(), padding=12)
            frame.grid(row=0, column=col, sticky="nsew", padx=(0, 8) if col == 0 else (8, 0))
            fields: dict[str, tk.StringVar] = {}
            defaults = {"host": "127.0.0.1", "port": "3306", "database": "", "user": ""}
            for row, key in enumerate(("host", "port", "database", "user")):
                var = tk.StringVar(value=defaults[key])
                fields[key] = var
                ttk.Label(frame, text=key.title()).grid(row=row, column=0, sticky="w", pady=5)
                ttk.Entry(frame, textvariable=var).grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=5)
            password = tk.StringVar()
            self.password_fields[role] = password
            ttk.Label(frame, text="Password").grid(row=4, column=0, sticky="w", pady=5)
            ttk.Entry(frame, textvariable=password, show="•").grid(row=4, column=1, sticky="ew", padx=(8, 0), pady=5)
            ttk.Label(frame, text="Leave blank to use password_env from config.", wraplength=300).grid(
                row=5, column=0, columnspan=2, sticky="w", pady=(4, 0)
            )
            frame.columnconfigure(1, weight=1)
            self.connection_fields[role] = fields
        self.connection_tab.columnconfigure(0, weight=1)
        self.connection_tab.columnconfigure(1, weight=1)

    def _build_tables(self) -> None:
        toolbar = ttk.Frame(self.tables_tab)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Button(toolbar, text="Select all", command=self._select_all).pack(side="left")
        ttk.Button(toolbar, text="Clear selection", command=self._clear_selection).pack(side="left", padx=8)
        ttk.Checkbutton(toolbar, text="Include referenced tables automatically", variable=self.include_dependencies).pack(side="left", padx=12)
        ttk.Checkbutton(toolbar, text="Allow source table locks for non-InnoDB", variable=self.allow_locks).pack(side="left", padx=12)
        body = ttk.Frame(self.tables_tab)
        body.pack(fill="both", expand=True)
        self.table_list = tk.Listbox(body, selectmode="extended", exportselection=False)
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.table_list.yview)
        self.table_list.configure(yscrollcommand=scrollbar.set)
        self.table_list.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        ttk.Label(self.tables_tab, text="Select one or more tables. Foreign-key dependencies are validated before copy.").pack(anchor="w", pady=(8, 0))

    def _build_rules(self) -> None:
        toolbar = ttk.Frame(self.rules_tab)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Button(toolbar, text="Load rules", command=self._load_rules).pack(side="left")
        ttk.Button(toolbar, text="Generate starter rules", command=self._generate_rules).pack(side="left", padx=8)
        ttk.Button(toolbar, text="Save rules", command=self._save_rules).pack(side="left")
        self.rules_editor = tk.Text(self.rules_tab, wrap="none", undo=True)
        self.rules_editor.pack(fill="both", expand=True)
        ttk.Label(self.rules_tab, text="Review generated rules carefully before copying. The editor accepts the same JSON format as rules.json.").pack(anchor="w", pady=(8, 0))

    def _choose_config(self) -> None:
        path = filedialog.askopenfilename(title="Open config", filetypes=(("JSON files", "*.json"), ("All files", "*")))
        if path:
            self.config_path.set(path)
            self._load_config()

    def _choose_rules(self) -> None:
        path = filedialog.askopenfilename(title="Open rules", filetypes=(("JSON files", "*.json"), ("All files", "*")))
        if path:
            self.rules_path.set(path)
            self._load_rules()

    def _choose_mapping(self) -> None:
        path = filedialog.asksaveasfilename(title="Mapping store path", defaultextension=".sqlite3")
        if path:
            self.mapping_path.set(path)

    def _load_config(self) -> None:
        try:
            config = load_json(self.config_path.get())
            for role in ("source", "destination"):
                settings = config.get(role, {})
                for key, var in self.connection_fields[role].items():
                    var.set(str(settings.get(key, "127.0.0.1" if key == "host" else "3306" if key == "port" else "")))
                self.password_fields[role].set("")
            self.allow_locks.set(bool(config.get("allow_source_table_locks", False)))
            self._log(f"Loaded config from {self.config_path.get()}. Passwords remain in the configured environment variables.")
        except Exception as exc:
            messagebox.showerror("Could not load config", str(exc))

    def _config(self) -> dict[str, Any]:
        try:
            config = load_json(self.config_path.get()) if Path(self.config_path.get()).exists() else {}
        except Exception:
            config = {}
        for role in ("source", "destination"):
            old = dict(config.get(role, {}))
            for key, var in self.connection_fields[role].items():
                value = var.get().strip()
                old[key] = int(value) if key == "port" and value else value
            password = self.password_fields[role].get()
            if password:
                old["password"] = password
                old.pop("password_env", None)
            config[role] = old
        config["allow_source_table_locks"] = self.allow_locks.get()
        check_config(config)
        return config

    def _save_config(self) -> None:
        try:
            config = self._config()
            for role in ("source", "destination"):
                config[role].pop("password", None)
            with open(self.config_path.get(), "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)
                f.write("\n")
            self._log(f"Saved connection settings to {self.config_path.get()}. Password fields were not saved.")
        except Exception as exc:
            messagebox.showerror("Could not save config", str(exc))

    def _load_rules(self) -> None:
        try:
            rules = load_json(self.rules_path.get())
            self._set_rules_text(json.dumps(rules, indent=2, ensure_ascii=False))
            self._log(f"Loaded rules from {self.rules_path.get()}.")
        except Exception as exc:
            messagebox.showerror("Could not load rules", str(exc))

    def _generate_rules(self) -> None:
        if not self.tables:
            messagebox.showinfo("Inspect source first", "Inspect the source database before generating rules.")
            return
        self._set_rules_text(json.dumps(init_rules(self.tables), indent=2, ensure_ascii=False))
        self.tabs_widget.select(self.rules_tab)
        self._log("Generated starter rules. Review every table and column before copying.")

    def _save_rules(self) -> None:
        try:
            rules = json.loads(self.rules_editor.get("1.0", "end-1c"))
            if not isinstance(rules, dict):
                raise ValueError("Rules file must contain a JSON object")
            save_json_atomic(self.rules_path.get(), rules)
            self._log(f"Saved rules to {self.rules_path.get()}.")
        except Exception as exc:
            messagebox.showerror("Could not save rules", str(exc))

    def _set_rules_text(self, value: str) -> None:
        self.rules_editor.delete("1.0", "end")
        self.rules_editor.insert("1.0", value)

    def _inspect(self) -> None:
        try:
            config = self._config()
            self._run_async("Inspecting source schema…", self._inspect_worker, config)
        except Exception as exc:
            messagebox.showerror("Invalid connection settings", str(exc))

    def _inspect_worker(self, config: dict[str, Any]) -> None:
        source = connect(config["source"])
        try:
            tables = inspect_schema(source, config["source"]["database"])
            self.messages.put(("tables", tables))
        finally:
            source.close()

    def _copy(self) -> None:
        if not self.tables:
            messagebox.showinfo("Inspect source first", "Inspect the source database before copying.")
            return
        if not messagebox.askyesno("Confirm copy", "Copy selected data into the empty destination? Password hashes may be retained according to the rules."):
            return
        try:
            raw = self.rules_editor.get("1.0", "end-1c").strip()
            rules = json.loads(raw) if raw else load_json(self.rules_path.get())
            if not isinstance(rules, dict):
                raise ValueError("Rules file must contain a JSON object")
            if not rules.get("confirm_copy_password_hashes"):
                raise ValueError("Set confirm_copy_password_hashes=true in the rules after reviewing the credential risk")
            selected = [self.table_names[i] for i in self.table_list.curselection()]
            if not selected:
                messagebox.showinfo("Select tables", "Select at least one table to copy.")
                return
            config = self._config()
            mapping_path = self.mapping_path.get().strip() or None
            rules_path = self.rules_path.get()
            allow_locks = self.allow_locks.get()
            include_dependencies = self.include_dependencies.get()
            self._run_async("Copying selected tables…", self._copy_worker, config, rules, selected,
                            mapping_path, rules_path, allow_locks, include_dependencies)
        except Exception as exc:
            messagebox.showerror("Could not start copy", str(exc))

    def _copy_worker(self, config: dict[str, Any], rules: dict[str, Any], selected: list[str],
                     mapping_path: str | None, rules_path: str, allow_locks: bool,
                     include_dependencies: bool) -> None:
        source = connect(config["source"])
        destination = None
        try:
            tables = inspect_schema(source, config["source"]["database"])
            moved = migrate_identifier_rules(rules, tables)
            if moved:
                save_json_atomic(rules_path, rules)
                self.messages.put(("log", "Moved identifier columns to preserve: " + ", ".join(moved)))
            destination = connect(config["destination"])
            cur = destination.cursor()
            cur.execute("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s",
                        (config["destination"]["database"],))
            table_count = cur.fetchone()[0]
            cur.close()
            if table_count:
                raise ValueError("Destination schema must be empty; nothing was changed")
            counts = copy_database(
                source, destination, config["source"]["database"], rules,
                config.get("batch_size", 500), allow_locks,
                mapping_path, selected, include_dependencies,
            )
            copied = set(counts)
            not_copied = [table["name"] for table in tables if table["name"] not in copied]
            report = ["Copy complete.", "Copied tables:"]
            report.extend(f"  {name}: {count} rows" for name, count in counts.items())
            report.append("Not copied tables:")
            report.extend(f"  {name}" for name in not_copied) if not_copied else report.append("  (none)")
            self.messages.put(("log", "\n".join(report)))
        finally:
            if destination is not None:
                destination.close()
            source.close()

    def _run_async(self, status: str, operation, *args) -> None:
        if self.busy:
            return
        self.busy = True
        self._set_busy(True)
        self.status.set(status)

        def run() -> None:
            try:
                operation(*args)
                self.messages.put(("done", None))
            except Exception as exc:
                self.messages.put(("error", str(exc)))

        threading.Thread(target=run, daemon=True).start()

    def _poll_messages(self) -> None:
        while True:
            try:
                kind, value = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == "tables":
                self.tables = value
                self.table_names = [table["name"] for table in self.tables]
                self.table_list.delete(0, "end")
                for table in self.tables:
                    refs = table.get("references", [])
                    suffix = f"  [references: {', '.join(refs)}]" if refs else ""
                    self.table_list.insert("end", f"{table['name']}{suffix}")
                self._log(f"Found {len(self.tables)} source tables.")
            elif kind == "log":
                self._log(value)
            elif kind == "error":
                self.status.set("Operation failed.")
                self._log("Error: " + value)
                messagebox.showerror("Operation failed", value)
            elif kind == "done":
                self.status.set("Ready.")
            if kind in {"error", "done"}:
                self.busy = False
                self._set_busy(False)
        self.after(100, self._poll_messages)

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for widget in (self.save_config_button, self.inspect_button, self.copy_button):
            widget.configure(state=state)

    def _select_all(self) -> None:
        self.table_list.select_set(0, "end")

    def _clear_selection(self) -> None:
        self.table_list.selection_clear(0, "end")

    def _log(self, message: str) -> None:
        self.output.configure(state="normal")
        self.output.insert("end", message + "\n")
        self.output.see("end")
        self.output.configure(state="disabled")


def main() -> None:
    app = AnonymizerApp()
    if Path(app.config_path.get()).exists():
        app._load_config()
    if Path(app.rules_path.get()).exists():
        app._load_rules()
    app.mainloop()


if __name__ == "__main__":
    main()
