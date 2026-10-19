from __future__ import annotations

import json
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
    list_mapping_fields,
    lookup_mapping,
    migrate_identifier_rules,
    save_json_atomic,
)


def connect_with_guidance(role: str, settings: dict[str, Any]):
    try:
        return connect(settings)
    except Exception as exc:
        host = settings.get("host", "localhost")
        port = settings.get("port", 3306)
        database = settings.get("database", "<не указана>")
        user = settings.get("user", "<не указан>")
        errno = getattr(exc, "errno", None)
        if errno == 1045:
            hint = ("MySQL отклонил аутентификацию (ошибка 1045). Проверьте, что пользователь существует "
                    "для указанного host, пароль верен и аккаунту разрешено подключаться с этой машины. "
                    "По этой ошибке нельзя отличить отсутствующего пользователя от неверного пароля или host.")
        elif errno == 1044:
            hint = ("MySQL не разрешил пользователю доступ к указанной базе (ошибка 1044). "
                    "Проверьте GRANT-права пользователя на эту базу.")
        elif errno == 1049:
            hint = "MySQL не нашёл указанную базу (ошибка 1049). Проверьте имя базы в настройках."
        else:
            hint = "Проверьте, что сервер доступен, порт верен, а пользователь и его права настроены правильно."
        raise RuntimeError(
            f"Не удалось подключиться к {role}: база {database!r}, пользователь {user!r}, "
            f"сервер {host}:{port}. {hint}\nДетали MySQL: {exc}"
        ) from exc


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
        self.lookup_tab = ttk.Frame(self.tabs_widget, padding=16)
        self.browse_tab = ttk.Frame(self.tabs_widget, padding=12)
        self.tabs_widget.add(self.connection_tab, text="Connections")
        self.tabs_widget.add(self.tables_tab, text="Tables")
        self.tabs_widget.add(self.rules_tab, text="Anonymization rules")
        self.tabs_widget.add(self.lookup_tab, text="Lookup")
        self.tabs_widget.add(self.browse_tab, text="Browse data")
        self._build_connections()
        self._build_tables()
        self._build_rules()
        self._build_lookup()
        self._build_browse()

        footer = ttk.Frame(self, padding=(12, 4, 12, 12))
        footer.pack(fill="both")
        buttons = ttk.Frame(footer)
        buttons.pack(fill="x", pady=(0, 8))
        self.save_config_button = ttk.Button(buttons, text="Save config", command=self._save_config)
        self.save_config_button.pack(side="left")
        self.inspect_button = ttk.Button(buttons, text="Inspect source", command=self._inspect)
        self.inspect_button.pack(side="left", padx=8)
        self.test_connections_button = ttk.Button(buttons, text="Test connections", command=self._test_connections)
        self.test_connections_button.pack(side="left")
        self.copy_button = ttk.Button(buttons, text="Copy selected tables", command=self._copy)
        self.copy_button.pack(side="right")
        self.status = tk.StringVar(value="Load or enter connection settings, then inspect the source.")
        self.progress = ttk.Progressbar(footer, mode="determinate", maximum=1, value=0)
        self.progress.pack(fill="x", pady=(0, 4))
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

    def _build_lookup(self) -> None:
        self.lookup_table = tk.StringVar()
        self.lookup_column = tk.StringVar()
        self.lookup_value = tk.StringVar()
        self.lookup_direction = tk.StringVar(value="original-to-anonymized")
        self.lookup_fields_by_table: dict[str, list[str]] = {}
        form = ttk.Frame(self.lookup_tab)
        form.pack(anchor="nw", fill="x")
        ttk.Label(form, text="Table", width=18).grid(row=0, column=0, sticky="w", pady=6)
        self.lookup_table_box = ttk.Combobox(form, textvariable=self.lookup_table, state="readonly", width=58)
        self.lookup_table_box.grid(row=0, column=1, sticky="ew", pady=6)
        self.lookup_table_box.bind("<<ComboboxSelected>>", self._lookup_table_selected)
        ttk.Label(form, text="Column", width=18).grid(row=1, column=0, sticky="w", pady=6)
        self.lookup_column_box = ttk.Combobox(form, textvariable=self.lookup_column, state="readonly", width=58)
        self.lookup_column_box.grid(row=1, column=1, sticky="ew", pady=6)
        ttk.Label(form, text="Exact value", width=18).grid(row=2, column=0, sticky="w", pady=6)
        ttk.Entry(form, textvariable=self.lookup_value, width=60).grid(row=2, column=1, sticky="ew", pady=6)
        ttk.Label(form, text="Direction", width=18).grid(row=3, column=0, sticky="nw", pady=6)
        directions = ttk.Frame(form)
        directions.grid(row=3, column=1, sticky="w", pady=6)
        ttk.Radiobutton(directions, text="Original → anonymized", value="original-to-anonymized",
                        variable=self.lookup_direction).pack(anchor="w")
        ttk.Radiobutton(directions, text="Anonymized → original", value="anonymized-to-original",
                        variable=self.lookup_direction).pack(anchor="w")
        form.columnconfigure(1, weight=1)
        lookup_buttons = ttk.Frame(form)
        lookup_buttons.grid(row=4, column=1, sticky="w", pady=(10, 0))
        self.lookup_fields_button = ttk.Button(lookup_buttons, text="Load fields", command=self._load_lookup_fields)
        self.lookup_fields_button.pack(side="left")
        ttk.Button(lookup_buttons, text="Find mapping", command=self._lookup).pack(side="left", padx=(8, 0))
        ttk.Label(self.lookup_tab,
                  text="Uses the SQLite mapping store selected above. Search is exact and may return multiple values.",
                  wraplength=760).pack(anchor="w", pady=(16, 0))

    def _build_browse(self) -> None:
        self.browse_role = tk.StringVar(value="source")
        self.browse_table = tk.StringVar()
        self.browse_search = tk.StringVar()
        self.browse_column = tk.StringVar()
        self.browse_column_value = tk.StringVar()
        self.browse_tables_by_role: dict[str, list[dict[str, Any]]] = {}
        controls = ttk.Frame(self.browse_tab)
        controls.pack(fill="x", pady=(0, 8))
        ttk.Label(controls, text="Database").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=3)
        self.browse_role_box = ttk.Combobox(controls, textvariable=self.browse_role,
                                            values=("source", "destination"), state="readonly", width=12)
        self.browse_role_box.grid(row=0, column=1, sticky="w", pady=3)
        self.browse_role_box.bind("<<ComboboxSelected>>", self._browse_role_selected)
        self.browse_tables_button = ttk.Button(controls, text="Load tables", command=self._browse_load_tables)
        self.browse_tables_button.grid(row=0, column=2, padx=8, pady=3)
        ttk.Label(controls, text="Table").grid(row=0, column=3, sticky="w", padx=(8, 6), pady=3)
        self.browse_table_box = ttk.Combobox(controls, textvariable=self.browse_table, state="readonly", width=28)
        self.browse_table_box.grid(row=0, column=4, sticky="ew", pady=3)
        self.browse_table_box.bind("<<ComboboxSelected>>", self._browse_table_selected)
        ttk.Label(controls, text="Search all columns").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=3)
        ttk.Entry(controls, textvariable=self.browse_search).grid(row=1, column=1, columnspan=4, sticky="ew", pady=3)
        ttk.Label(controls, text="Column filter").grid(row=2, column=0, sticky="w", padx=(0, 6), pady=3)
        self.browse_column_box = ttk.Combobox(controls, textvariable=self.browse_column, state="readonly", width=24)
        self.browse_column_box.grid(row=2, column=1, sticky="ew", pady=3)
        ttk.Entry(controls, textvariable=self.browse_column_value).grid(row=2, column=2, columnspan=2, sticky="ew", padx=(8, 0), pady=3)
        self.browse_search_button = ttk.Button(controls, text="Search / refresh", command=self._browse_search)
        self.browse_search_button.grid(row=2, column=4, sticky="e", padx=(8, 0), pady=3)
        controls.columnconfigure(4, weight=1)

        grid = ttk.Frame(self.browse_tab)
        grid.pack(fill="both", expand=True)
        self.browse_tree = ttk.Treeview(grid, show="headings")
        yscroll = ttk.Scrollbar(grid, orient="vertical", command=self.browse_tree.yview)
        xscroll = ttk.Scrollbar(grid, orient="horizontal", command=self.browse_tree.xview)
        self.browse_tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.browse_tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        grid.rowconfigure(0, weight=1)
        grid.columnconfigure(0, weight=1)
        ttk.Label(self.browse_tab, text="Showing up to 200 matching rows. Search terms match anywhere within a value.").pack(anchor="w", pady=(6, 0))

    def _browse_load_tables(self) -> None:
        try:
            config = self._config()
            role = self.browse_role.get()
            self._run_async(f"Loading {role} tables…", self._browse_tables_worker,
                            role, config[role])
        except Exception as exc:
            messagebox.showerror("Invalid connection settings", str(exc))

    def _browse_tables_worker(self, role: str, settings: dict[str, Any]) -> None:
        conn = connect_with_guidance(role, settings)
        try:
            tables = inspect_schema(conn, settings["database"])
            self.messages.put(("browse_tables", (role, tables)))
        finally:
            conn.close()

    def _browse_role_selected(self, _event=None) -> None:
        self.browse_table.set("")
        self.browse_table_box.configure(values=())
        self.browse_column.set("")
        self.browse_column_box.configure(values=("",))

    def _browse_table_selected(self, _event=None) -> None:
        self._browse_update_columns()
        self._browse_search()

    def _browse_update_columns(self) -> None:
        role = self.browse_role.get()
        table_name = self.browse_table.get()
        table = next((item for item in self.browse_tables_by_role.get(role, [])
                      if item["name"] == table_name), None)
        columns = [column["COLUMN_NAME"] for column in table["columns"]] if table else []
        self.browse_column_box.configure(values=[""] + columns)
        self.browse_column.set("")

    def _browse_search(self) -> None:
        table_name = self.browse_table.get()
        role = self.browse_role.get()
        table = next((item for item in self.browse_tables_by_role.get(role, [])
                      if item["name"] == table_name), None)
        if not table:
            messagebox.showinfo("Select a table", "Load tables and choose a table first.")
            return
        column = self.browse_column.get().strip()
        columns = [item["COLUMN_NAME"] for item in table["columns"]]
        if column and column not in columns:
            messagebox.showerror("Invalid column", "Choose a column from the selected table.")
            return
        try:
            config = self._config()
            self._run_async(f"Reading {role}.{table_name}…", self._browse_query_worker,
                            role, config[role], table_name, columns, self.browse_search.get(),
                            column, self.browse_column_value.get())
        except Exception as exc:
            messagebox.showerror("Invalid connection settings", str(exc))

    def _browse_query_worker(self, role: str, settings: dict[str, Any], table: str,
                             columns: list[str], search: str, filter_column: str,
                             filter_value: str) -> None:
        conn = connect_with_guidance(role, settings)
        cur = conn.cursor()
        try:
            conditions, params = [], []
            if search:
                conditions.append("(" + " OR ".join(
                    f"CAST(`{name.replace('`', '``')}` AS CHAR) LIKE %s" for name in columns
                ) + ")")
                params.extend([f"%{search}%"] * len(columns))
            if filter_column and filter_value:
                conditions.append(f"CAST(`{filter_column.replace('`', '``')}` AS CHAR) LIKE %s")
                params.append(f"%{filter_value}%")
            where = " WHERE " + " AND ".join(conditions) if conditions else ""
            cur.execute(f"SELECT * FROM `{table.replace('`', '``')}`{where} LIMIT 201", tuple(params))
            rows = cur.fetchall()
            self.messages.put(("browse_rows", (table, columns, rows[:200], len(rows) > 200)))
        finally:
            cur.close()
            conn.close()

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
        path = filedialog.askopenfilename(
            title="Open mapping store",
            filetypes=(("SQLite databases", "*.sqlite *.sqlite3 *.db"), ("All files", "*")),
        )
        if path:
            self.mapping_path.set(path)
            self._load_lookup_fields()

    def _load_lookup_fields(self) -> None:
        path = self.mapping_path.get().strip()
        if not path:
            messagebox.showinfo("Select mapping store", "Choose the SQLite mapping store first.")
            return
        self._run_async("Loading mapping tables and columns…", self._lookup_fields_worker, path)

    def _lookup_fields_worker(self, path: str) -> None:
        fields = list_mapping_fields(path)
        self.messages.put(("lookup_fields", fields))

    def _lookup_table_selected(self, _event=None) -> None:
        columns = self.lookup_fields_by_table.get(self.lookup_table.get(), [])
        self.lookup_column_box.configure(values=columns)
        self.lookup_column.set(columns[0] if columns else "")

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

    def _test_connections(self) -> None:
        try:
            config = self._config()
            self._run_async("Testing source and destination connections…", self._test_connections_worker, config)
        except Exception as exc:
            messagebox.showerror("Invalid connection settings", str(exc))

    def _test_connections_worker(self, config: dict[str, Any]) -> None:
        reports = []
        for role in ("source", "destination"):
            conn = None
            try:
                conn = connect_with_guidance(role, config[role])
                reports.append(f"✓ {role}: подключение успешно ({config[role].get('database')}).")
            except Exception as exc:
                reports.append(f"✗ {role}: {exc}")
            finally:
                if conn is not None:
                    conn.close()
        self.messages.put(("connection_report", "\n\n".join(reports)))

    def _inspect_worker(self, config: dict[str, Any]) -> None:
        source = connect_with_guidance("source", config["source"])
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

    def _lookup(self) -> None:
        path = self.mapping_path.get().strip()
        table = self.lookup_table.get().strip()
        column = self.lookup_column.get().strip()
        value = self.lookup_value.get()
        if not path:
            messagebox.showinfo("Select mapping store", "Choose the SQLite mapping store in the field above.")
            return
        if not table or not column:
            messagebox.showinfo("Complete lookup fields", "Select a table and column to search.")
            return
        self._run_async("Searching mapping store…", self._lookup_worker, path, table, column,
                        value, self.lookup_direction.get())

    def _lookup_worker(self, path: str, table: str, column: str, value: str, direction: str) -> None:
        results = lookup_mapping(path, table, column, value, direction)
        self.messages.put(("lookup_results", (direction, table, column, value, results)))

    def _copy_worker(self, config: dict[str, Any], rules: dict[str, Any], selected: list[str],
                     mapping_path: str | None, rules_path: str, allow_locks: bool,
                     include_dependencies: bool) -> None:
        source = connect_with_guidance("source", config["source"])
        destination = None
        try:
            tables = inspect_schema(source, config["source"]["database"])
            moved = migrate_identifier_rules(rules, tables)
            if moved:
                save_json_atomic(rules_path, rules)
                self.messages.put(("log", "Moved identifier columns to preserve: " + ", ".join(moved)))
            destination = connect_with_guidance("destination", config["destination"])
            counts = copy_database(
                source, destination, config["source"]["database"], rules,
                config.get("batch_size", 500), allow_locks,
                mapping_path, selected, include_dependencies,
                lambda progress: self.messages.put(("progress", progress)),
            )
            if mapping_path:
                self.messages.put(("lookup_fields", list_mapping_fields(mapping_path)))
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
            elif kind == "progress":
                self._show_progress(value)
            elif kind == "browse_tables":
                role, tables = value
                self.browse_tables_by_role[role] = tables
                names = [table["name"] for table in tables]
                self.browse_table_box.configure(values=names)
                self.browse_table.set(names[0] if names else "")
                self._log(f"Loaded {len(names)} {role} table(s) for browsing.")
                if names:
                    self._browse_update_columns()
                else:
                    self._clear_browse_rows()
            elif kind == "browse_rows":
                table, columns, rows, truncated = value
                self._display_browse_rows(columns, rows)
                suffix = "; showing the first 200 rows, refine the search" if truncated else ""
                self.status.set(f"Showing {len(rows)} row(s) from {table}{suffix}.")
            elif kind == "connection_report":
                self._log(value)
                messagebox.showinfo("Connection test", value)
            elif kind == "lookup_results":
                direction, table, column, value, results = value
                if results:
                    report = [f"Lookup results ({direction}) for {table}.{column} = {value!r}:"]
                    report.extend(f"  {result}" for result in results)
                else:
                    report = [f"No matching value found for {table}.{column} = {value!r}."]
                self._log("\n".join(report))
            elif kind == "lookup_fields":
                self.lookup_fields_by_table = value
                tables = list(value)
                self.lookup_table_box.configure(values=tables)
                self.lookup_table.set(tables[0] if tables else "")
                self._lookup_table_selected()
                self._log(f"Loaded {len(tables)} mapped table(s) and their columns.")
            elif kind == "error":
                self.status.set("Operation failed.")
                self._log("Error: " + value)
                messagebox.showerror("Operation failed", value)
            elif kind == "done":
                if not self.status.get().startswith("Showing "):
                    self.status.set("Ready.")
                self.progress.configure(value=self.progress.cget("maximum"))
            if kind in {"error", "done"}:
                self.busy = False
                self._set_busy(False)
        self.after(100, self._poll_messages)

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for widget in (self.save_config_button, self.inspect_button, self.test_connections_button,
                       self.copy_button, self.browse_tables_button, self.browse_search_button,
                       self.lookup_fields_button):
            widget.configure(state=state)

    def _clear_browse_rows(self) -> None:
        self.browse_tree.delete(*self.browse_tree.get_children())
        self.browse_tree.configure(columns=())

    def _display_browse_rows(self, columns: list[str], rows: list[tuple[Any, ...]]) -> None:
        self.browse_tree.delete(*self.browse_tree.get_children())
        identifiers = [f"column_{index}" for index in range(len(columns))]
        self.browse_tree.configure(columns=identifiers)
        for identifier, name in zip(identifiers, columns):
            self.browse_tree.heading(identifier, text=name)
            self.browse_tree.column(identifier, width=150, minwidth=80, stretch=True)
        for row in rows:
            values = []
            for value in row:
                if value is None:
                    display = "NULL"
                elif isinstance(value, bytes):
                    display = "0x" + value[:100].hex() + ("…" if len(value) > 100 else "")
                else:
                    display = str(value)
                values.append(display[:500] + ("…" if len(display) > 500 else ""))
            self.browse_tree.insert("", "end", values=values)

    def _show_progress(self, progress: dict[str, Any]) -> None:
        event = progress["event"]
        total_rows = progress.get("total_rows", 0)
        total_tables = progress.get("total_tables", 0)
        if event == "start":
            maximum = total_rows if total_rows else max(total_tables, 1)
            self.progress.configure(maximum=maximum, value=0)
            self.status.set(f"Copying {total_tables} table(s), {total_rows} row(s)…")
            return
        table = progress.get("table", "")
        table_index = progress.get("table_index", 0)
        table_rows = progress.get("table_rows", 0)
        copied_rows = progress.get("copied_rows", 0)
        if total_rows:
            self.progress.configure(value=progress.get("completed_rows", 0))
        elif event == "table_done":
            self.progress.configure(value=progress.get("completed_tables", 0))
        self.status.set(
            f"Copying table {table_index}/{total_tables}: {table} "
            f"({copied_rows}/{table_rows} rows; {progress.get('completed_rows', 0)}/{total_rows} total)"
        )

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
