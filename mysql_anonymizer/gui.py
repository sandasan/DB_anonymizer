from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from .cli import check_config
from .core import (
    CopyPaused,
    connect,
    copy_database,
    init_rules,
    inspect_schema,
    load_json,
    list_mapping_fields,
    lookup_mapping,
    migrate_identifier_rules,
    open_mapping_store,
    prune_rules_to_schema,
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
        self.geometry("1120x820")
        self.minsize(900, 680)
        self._configure_style()
        self.tables: list[dict[str, Any]] = []
        self.table_names: list[str] = []
        self.copied_table_names: set[str] = set()
        self.source_inspected = False
        self.messages: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.busy = False
        self.copy_started_at: float | None = None
        self.pause_event: threading.Event | None = None
        self.copy_in_progress = False
        self.close_when_paused = False
        self.copy_resume_state: dict[str, Any] | None = None

        self.config_path = tk.StringVar(value="config.json")
        self.rules_path = tk.StringVar(value="rules.json")
        self._load_last_paths()
        self.mapping_path = tk.StringVar()
        self.allow_locks = tk.BooleanVar(value=False)
        self.include_dependencies = tk.BooleanVar(value=False)
        self.connection_fields: dict[str, dict[str, tk.StringVar]] = {}
        self.password_fields: dict[str, tk.StringVar] = {}
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_messages)
        self.after(250, self._offer_resume_copy)

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        self.configure(background="#f3f5f8")
        style.configure("TFrame", background="#f3f5f8")
        style.configure("TLabel", background="#f3f5f8", foreground="#253247", font=("TkDefaultFont", 10))
        style.configure("Heading.TLabel", font=("TkDefaultFont", 11, "bold"), foreground="#152238")
        style.configure("TLabelframe", background="#f3f5f8", bordercolor="#d6dde7", relief="solid")
        style.configure("TLabelframe.Label", background="#f3f5f8", foreground="#34445c", font=("TkDefaultFont", 10, "bold"))
        style.configure("TButton", padding=(13, 8), font=("TkDefaultFont", 9))
        style.map("TButton", foreground=[("disabled", "#8a94a3")])
        style.configure("Accent.TButton", background="#2563eb", foreground="white", padding=(16, 9), font=("TkDefaultFont", 9, "bold"))
        style.map("Accent.TButton", background=[("active", "#1d4ed8"), ("disabled", "#a8b8d6")], foreground=[("disabled", "#eef2f8")])
        style.configure("TEntry", padding=6, fieldbackground="white")
        style.configure("TCombobox", padding=5, fieldbackground="white")
        style.configure("TNotebook", background="#f3f5f8", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(15, 9), font=("TkDefaultFont", 9, "bold"))
        style.map("TNotebook.Tab", background=[("selected", "white"), ("!selected", "#e7ebf1")], foreground=[("selected", "#1d4ed8"), ("!selected", "#536176")])
        style.configure("Treeview", background="white", fieldbackground="white", foreground="#29364a", rowheight=29, bordercolor="#dce2eb", borderwidth=1, font=("TkDefaultFont", 9))
        style.configure("Treeview.Heading", background="#e9eef5", foreground="#36445a", relief="flat", padding=(8, 8), font=("TkDefaultFont", 9, "bold"))
        style.map("Treeview", background=[("selected", "#dbeafe")], foreground=[("selected", "#173b78")])
        style.configure("Horizontal.TProgressbar", thickness=9, troughcolor="#e0e6ef", background="#3975e8")

    @staticmethod
    def _state_file() -> Path:
        if os.name == "nt":
            config_root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        elif sys.platform == "darwin":
            config_root = Path.home() / "Library" / "Application Support"
        else:
            config_root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        return config_root / "mysql-anonymized-copy" / "gui_state.json"

    def _load_last_paths(self) -> None:
        try:
            state = json.loads(self._state_file().read_text(encoding="utf-8"))
            config_path = state.get("config_path")
            rules_path = state.get("rules_path")
            if isinstance(config_path, str) and config_path:
                self.config_path.set(config_path)
            if isinstance(rules_path, str) and rules_path:
                self.rules_path.set(rules_path)
        except (OSError, json.JSONDecodeError, AttributeError):
            pass

    def _save_last_paths(self) -> None:
        path = self._state_file()
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "config_path": self.config_path.get(),
                "rules_path": self.rules_path.get(),
            }, indent=2) + "\n", encoding="utf-8")
            try:
                path.chmod(0o600)
            except OSError:
                pass
        except OSError as exc:
            self._log(f"Could not remember config and rules paths: {exc}")

    def _on_close(self) -> None:
        if self.copy_in_progress:
            self.close_when_paused = True
            if self.pause_event is not None:
                self.pause_event.set()
            self.status.set("Pausing at the next batch boundary; the app will close when the checkpoint is saved…")
            return
        self._save_last_paths()
        self.destroy()

    @classmethod
    def _resume_copy_path(cls) -> Path:
        return cls._state_file().with_name("copy_resume.json")

    def _save_copy_checkpoint(self, state: dict[str, Any]) -> None:
        path = self._resume_copy_path()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        save_json_atomic(str(path), state)
        path.chmod(0o600)

    def _offer_resume_copy(self) -> None:
        path = self._resume_copy_path()
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or state.get("version") != 1:
                return
        except (OSError, json.JSONDecodeError):
            return
        self.copy_resume_state = state
        self.copy_button.configure(state="disabled")
        self.pause_button.configure(text="Resume copy", state="normal")
        if messagebox.askyesno(
            "Resume paused copy?",
            "A copy was paused or interrupted. Resume it using the saved connection and rule settings? "
            "Any partially copied table will be cleared and copied again; completed tables are kept. "
            "The checkpoint contains credentials and is stored with owner-only file permissions.",
        ):
            self._resume_copy()

    def _resume_copy(self) -> None:
        state = self.copy_resume_state
        if not state or self.busy:
            return
        self.pause_event = threading.Event()
        self.copy_in_progress = True
        self.copy_button.configure(state="disabled")
        self.pause_button.configure(text="Pause copy", state="normal")
        self.mapping_path.set(state.get("mapping_path") or "")
        self._run_async("Resuming copy…", self._copy_worker,
                        state["config"], state["rules"], state["selected"],
                        state.get("mapping_path"), state.get("rules_path"),
                        state.get("allow_locks", False), state.get("include_dependencies", False),
                        state, self.pause_event)

    def _request_pause(self) -> None:
        if self.copy_in_progress and self.pause_event is not None:
            self.pause_event.set()
            self.pause_button.configure(state="disabled")
            self.status.set("Pausing after the current batch is safely saved…")
        elif self.copy_resume_state is not None:
            self._resume_copy()

    def _build_ui(self) -> None:
        paths = ttk.Frame(self, padding=(12, 12, 12, 4))
        paths.pack(fill="x")
        self._config_path_row(paths, 0)
        self._rules_path_row(paths, 1)
        self._path_row(paths, 2, "Mapping store (optional)", self.mapping_path, self._choose_mapping)

        actions = ttk.Frame(self, padding=(12, 4, 12, 4))
        actions.pack(fill="x")
        self.save_config_button = ttk.Button(actions, text="Save config", command=self._save_config, width=14)
        self.save_config_button.pack(side="left")
        self.inspect_button = ttk.Button(actions, text="Inspect source", command=self._inspect, width=16)
        self.inspect_button.pack(side="left", padx=8)
        self.test_connections_button = ttk.Button(actions, text="Test connections", command=self._test_connections, width=17)
        self.test_connections_button.pack(side="left")
        self.copy_button = ttk.Button(actions, text="Copy selected tables", command=self._copy, style="Accent.TButton", width=22)
        self.copy_button.pack(side="right")
        self.pause_button = ttk.Button(actions, text="Pause copy", command=self._request_pause, state="disabled", width=14)
        self.pause_button.pack(side="right", padx=(0, 8))

        progress_panel = ttk.Frame(self, padding=(12, 0, 12, 4))
        progress_panel.pack(fill="x")
        ttk.Label(progress_panel, text="Overall copy progress", style="Heading.TLabel").pack(anchor="w")
        self.overall_progress = ttk.Progressbar(progress_panel, mode="determinate", maximum=1, value=0)
        self.overall_progress.pack(fill="x", pady=(0, 3))
        ttk.Label(progress_panel, text="Current table", style="Heading.TLabel").pack(anchor="w")
        self.table_progress = ttk.Progressbar(progress_panel, mode="determinate", maximum=1, value=0)
        self.table_progress.pack(fill="x", pady=(0, 4))
        self.status = tk.StringVar(value="Load or enter connection settings, then inspect the source.")
        ttk.Label(progress_panel, textvariable=self.status, foreground="#52637a").pack(anchor="w", pady=(2, 0))

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

        footer = ttk.Frame(self, padding=(12, 4, 12, 8))
        footer.pack(fill="x")
        self.output = tk.Text(footer, height=4, wrap="word", state="disabled")
        self.output.configure(background="#fbfcfe", foreground="#435168", relief="flat", padx=9, pady=7,
                              font=("TkFixedFont", 9), highlightthickness=1, highlightbackground="#dce2eb")
        self.output.pack(fill="x")

    def _config_path_row(self, parent: ttk.Frame, row: int) -> None:
        ttk.Label(parent, text="Config", width=22).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(parent, textvariable=self.config_path).grid(row=row, column=1, sticky="ew", padx=8, pady=2)
        ttk.Button(parent, text="Load config…", command=self._choose_config).grid(row=row, column=2, padx=(0, 4), pady=2)
        ttk.Button(parent, text="Save config as…", command=self._save_config_as).grid(row=row, column=3, pady=2)
        parent.columnconfigure(1, weight=1)

    def _rules_path_row(self, parent: ttk.Frame, row: int) -> None:
        ttk.Label(parent, text="Rules", width=22).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(parent, textvariable=self.rules_path).grid(row=row, column=1, sticky="ew", padx=8, pady=2)
        ttk.Button(parent, text="Load rules…", command=self._choose_rules).grid(row=row, column=2, padx=(0, 4), pady=2)
        ttk.Button(parent, text="Save rules as…", command=self._save_rules_as).grid(row=row, column=3, pady=2)
        parent.columnconfigure(1, weight=1)

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
        self.table_list = ttk.Treeview(
            body, columns=("status", "table", "records", "size", "eta", "references"),
            show="headings", selectmode="extended",
        )
        for column, heading, width in (
            ("status", "Destination", 90), ("table", "Table", 190),
            ("records", "Records", 110), ("size", "Volume", 100),
            ("eta", "Copy ETA", 110), ("references", "References", 210),
        ):
            self.table_list.heading(column, text=heading)
            self.table_list.column(column, width=width, minwidth=70,
                                   anchor="w" if column in {"table", "references"} else "center")
        yscroll = ttk.Scrollbar(body, orient="vertical", command=self.table_list.yview)
        xscroll = ttk.Scrollbar(body, orient="horizontal", command=self.table_list.xview)
        self.table_list.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.table_list.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        ttk.Label(self.tables_tab, text="✓ marks tables already present in the destination. Foreign-key dependencies are validated before copy.").pack(anchor="w", pady=(8, 0))

    def _build_rules(self) -> None:
        toolbar = ttk.Frame(self.rules_tab)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Button(toolbar, text="Load rules", command=self._load_rules).pack(side="left")
        ttk.Button(toolbar, text="Generate starter rules", command=self._generate_rules).pack(side="left", padx=8)
        ttk.Button(toolbar, text="Save rules", command=self._save_rules).pack(side="left")

        filters = ttk.Frame(self.rules_tab)
        filters.pack(fill="x", pady=(0, 6))
        ttk.Label(filters, text="Search").pack(side="left")
        self.rules_search = tk.StringVar()
        search_entry = ttk.Entry(filters, textvariable=self.rules_search, width=28)
        search_entry.pack(side="left", padx=(6, 12))
        ttk.Label(filters, text="Table").pack(side="left")
        self.rules_table_filter = tk.StringVar(value="All tables")
        self.rules_table_filter_box = ttk.Combobox(
            filters, textvariable=self.rules_table_filter, state="readonly", width=22,
            values=("All tables",),
        )
        self.rules_table_filter_box.pack(side="left", padx=(6, 12))
        self.rules_table_filter_box.bind("<<ComboboxSelected>>", lambda _event: self._refresh_rules_preview())
        ttk.Label(filters, text="Status").pack(side="left")
        self.rules_status_filter = tk.StringVar(value="All statuses")
        self.rules_preview_after_id = None
        self.rules_status_filter_box = ttk.Combobox(
            filters, textvariable=self.rules_status_filter, state="readonly", width=18,
            values=("All statuses", "Anonymized", "Preserved", "Unconfigured", "Needs attention"),
        )
        self.rules_status_filter_box.pack(side="left", padx=(6, 0))
        self.rules_status_filter_box.bind("<<ComboboxSelected>>", lambda _event: self._refresh_rules_preview())
        ttk.Button(filters, text="Reset filters", command=self._reset_rules_filters).pack(side="left", padx=(8, 0))
        self.rules_search.trace_add("write", lambda *_args: self._refresh_rules_preview())

        preview_frame = ttk.Frame(self.rules_tab)
        preview_frame.pack(fill="both", expand=True, pady=(0, 8))
        self.rules_preview = ttk.Treeview(
            preview_frame, columns=("table", "column", "status"), show="headings", height=8,
        )
        self.rules_preview.heading("table", text="Table")
        self.rules_preview.heading("column", text="Column")
        self.rules_preview.heading("status", text="Rule status")
        self.rules_preview.column("table", width=180, minwidth=100)
        self.rules_preview.column("column", width=180, minwidth=100)
        self.rules_preview.column("status", width=220, minwidth=140)
        preview_scroll = ttk.Scrollbar(preview_frame, orient="vertical", command=self.rules_preview.yview)
        self.rules_preview.configure(yscrollcommand=preview_scroll.set)
        self.rules_preview.pack(side="left", fill="both", expand=True)
        preview_scroll.pack(side="right", fill="y")
        self.rules_preview.bind("<Double-1>", self._rules_preview_navigate)
        self.rules_preview_status = tk.StringVar(value="Inspect the source to compare rules with its columns.")
        ttk.Label(self.rules_tab, textvariable=self.rules_preview_status).pack(anchor="w", pady=(0, 4))

        self.rules_editor = tk.Text(self.rules_tab, wrap="none", undo=True)
        self.rules_editor.pack(fill="both", expand=True)
        self.rules_editor.bind("<KeyRelease>", lambda _event: self._schedule_rules_preview())
        ttk.Label(self.rules_tab, text="Review generated rules carefully before copying. Use the navigator to find columns; edit the JSON below and save when ready.").pack(anchor="w", pady=(8, 0))
        self._refresh_rules_preview()

    def _reset_rules_filters(self) -> None:
        self.rules_search.set("")
        self.rules_table_filter.set("All tables")
        self.rules_status_filter.set("All statuses")
        self._refresh_rules_preview()

    def _rules_preview_rows(self) -> tuple[list[tuple[str, str, str]], list[str]]:
        raw = self.rules_editor.get("1.0", "end-1c").strip()
        rules = json.loads(raw) if raw else {"tables": {}}
        if not isinstance(rules, dict):
            raise ValueError("Rules must be a JSON object")
        table_rules = rules.get("tables", {})
        if not isinstance(table_rules, dict):
            raise ValueError("The 'tables' rules entry must be an object")
        source_by_name = {table["name"]: table for table in self.tables}
        table_names = sorted(set(source_by_name) | set(table_rules))
        rows: list[tuple[str, str, str]] = []
        for table_name in table_names:
            table_rule = table_rules.get(table_name, {})
            if not isinstance(table_rule, dict):
                continue
            preserve = table_rule.get("preserve", [])
            anonymize = table_rule.get("anonymize", {})
            if not isinstance(preserve, list):
                preserve = []
            if not isinstance(anonymize, dict):
                anonymize = {}
            source_table = source_by_name.get(table_name)
            preserve_columns = {column for column in preserve if isinstance(column, str)}
            anonymized_columns = set(anonymize)
            if self.source_inspected:
                source_columns = {column["COLUMN_NAME"] for column in source_table["columns"]} if source_table else set()
            else:
                source_columns = preserve_columns | anonymized_columns
            columns = sorted(source_columns | preserve_columns | set(anonymize))
            for column in columns:
                if self.source_inspected and source_table is None:
                    status = "Table not in source"
                elif self.source_inspected and column not in source_columns:
                    status = "Column not in source"
                elif column in preserve and column in anonymize:
                    status = "Conflict: preserve + anonymize"
                elif column in anonymize:
                    transform = anonymize[column]
                    kind = transform.get("type", "unknown") if isinstance(transform, dict) else "invalid rule"
                    status = f"Anonymized ({kind})"
                elif column in preserve:
                    status = "Preserved"
                else:
                    status = "Unconfigured"
                rows.append((table_name, str(column), status))
        return rows, table_names

    def _refresh_rules_preview(self) -> None:
        if not hasattr(self, "rules_preview"):
            return
        try:
            rows, table_names = self._rules_preview_rows()
        except (json.JSONDecodeError, ValueError) as exc:
            self.rules_preview.delete(*self.rules_preview.get_children())
            self.rules_table_filter_box.configure(values=("All tables",))
            self.rules_table_filter.set("All tables")
            self.rules_preview_status.set(f"Fix the rules JSON to refresh this list: {exc}")
            return

        table_values = ["All tables"] + table_names
        self.rules_table_filter_box.configure(values=table_values)
        if self.rules_table_filter.get() not in table_values:
            self.rules_table_filter.set("All tables")
        search = self.rules_search.get().casefold().strip()
        selected_table = self.rules_table_filter.get()
        selected_status = self.rules_status_filter.get()
        filtered = []
        for row in rows:
            table_name, column_name, status = row
            if selected_table != "All tables" and table_name != selected_table:
                continue
            if search and search not in " ".join(row).casefold():
                continue
            if selected_status == "Anonymized" and not status.startswith("Anonymized"):
                continue
            if selected_status == "Preserved" and status != "Preserved":
                continue
            if selected_status == "Unconfigured" and status != "Unconfigured":
                continue
            if selected_status == "Needs attention" and status not in {
                "Column not in source", "Table not in source", "Conflict: preserve + anonymize", "Unconfigured"
            }:
                continue
            filtered.append(row)

        self.rules_preview.delete(*self.rules_preview.get_children())
        for row in filtered:
            self.rules_preview.insert("", "end", values=row)
        self.rules_preview_status.set(f"Showing {len(filtered)} of {len(rows)} source and configured columns. Double-click a row to jump to its rule.")

    def _schedule_rules_preview(self) -> None:
        if self.rules_preview_after_id is not None:
            self.after_cancel(self.rules_preview_after_id)
        def refresh() -> None:
            self.rules_preview_after_id = None
            self._refresh_rules_preview()
        self.rules_preview_after_id = self.after(250, refresh)

    def _rules_preview_navigate(self, _event=None) -> None:
        selected = self.rules_preview.selection()
        if not selected:
            return
        table_name, column_name, _status = self.rules_preview.item(selected[0], "values")
        lines = self.rules_editor.get("1.0", "end-1c").splitlines()
        table_key = json.dumps(table_name, ensure_ascii=False)
        column_key = json.dumps(column_name, ensure_ascii=False)
        table_line = next((index for index, line in enumerate(lines)
                           if line.strip().startswith(table_key + ":")), None)
        target_line = table_line
        if table_line is not None:
            for index in range(table_line + 1, len(lines)):
                if lines[index].startswith("    ") and not lines[index].startswith("        "):
                    break
                if column_key in lines[index]:
                    target_line = index
                    break
        if target_line is not None:
            index = f"{target_line + 1}.0"
            self.rules_editor.tag_remove("rules_navigator_match", "1.0", "end")
            self.rules_editor.tag_add("rules_navigator_match", index, f"{index} lineend")
            self.rules_editor.tag_configure("rules_navigator_match", background="#fff1a8")
            self.rules_editor.see(index)
            self.rules_editor.focus_set()
            self.after(1400, lambda: self.rules_editor.tag_remove("rules_navigator_match", "1.0", "end"))

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

    def _save_config_as(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save config as",
            defaultextension=".json",
            filetypes=(("JSON files", "*.json"), ("All files", "*")),
        )
        if path:
            self.config_path.set(path)
            self._save_config()

    def _choose_rules(self) -> None:
        path = filedialog.askopenfilename(title="Open rules", filetypes=(("JSON files", "*.json"), ("All files", "*")))
        if path:
            self.rules_path.set(path)
            self._load_rules()

    def _save_rules_as(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save rules as",
            defaultextension=".json",
            filetypes=(("JSON files", "*.json"), ("All files", "*")),
        )
        if path:
            self.rules_path.set(path)
            self._save_rules()

    def _choose_mapping(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Create or select mapping store",
            defaultextension=".sqlite3",
            confirmoverwrite=False,
            filetypes=(("SQLite databases", "*.sqlite *.sqlite3 *.db"), ("All files", "*")),
        )
        if path:
            try:
                store = open_mapping_store(path)
                store.close()
                self.mapping_path.set(path)
                self._load_lookup_fields()
            except Exception as exc:
                messagebox.showerror("Could not create mapping store", str(exc))

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
                self.password_fields[role].set(str(settings.get("password", "")))
            self.allow_locks.set(bool(config.get("allow_source_table_locks", False)))
            self._save_last_paths()
            self._log(f"Loaded config from {self.config_path.get()}.")
        except Exception as exc:
            messagebox.showerror("Could not load config", str(exc))

    def _config(self) -> dict[str, Any]:
        try:
            config = load_json(self.config_path.get()) if Path(self.config_path.get()).exists() else {}
        except Exception:
            config = {}
        for role in ("source", "destination"):
            old = dict(config.get(role, {}))
            old.pop("password", None)
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
            with open(self.config_path.get(), "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)
                f.write("\n")
            self._log(f"Saved connection settings and entered passwords to {self.config_path.get()}.")
            self._save_last_paths()
        except Exception as exc:
            messagebox.showerror("Could not save config", str(exc))

    def _load_rules(self) -> None:
        try:
            rules = load_json(self.rules_path.get())
            self._set_rules_text(json.dumps(rules, indent=2, ensure_ascii=False))
            self._save_last_paths()
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
            self._save_last_paths()
        except Exception as exc:
            messagebox.showerror("Could not save rules", str(exc))

    def _set_rules_text(self, value: str) -> None:
        self.rules_editor.delete("1.0", "end")
        self.rules_editor.insert("1.0", value)
        self._refresh_rules_preview()

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
            cursor = source.cursor()
            cursor.execute(
                "SELECT TABLE_NAME, DATA_LENGTH, INDEX_LENGTH "
                "FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE'",
                (config["source"]["database"],),
            )
            sizes = {name: (data_length or 0) + (index_length or 0)
                     for name, data_length, index_length in cursor.fetchall()}
            for table in tables:
                cursor.execute(f"SELECT COUNT(*) FROM `{table['name'].replace('`', '``')}`")
                table["row_count"] = cursor.fetchone()[0]
                table["size_bytes"] = sizes.get(table["name"], 0)
            cursor.close()
        finally:
            source.close()
        destination_tables: set[str] = set()
        destination = None
        try:
            destination = connect_with_guidance("destination", config["destination"])
            cur = destination.cursor()
            cur.execute(
                "SELECT TABLE_NAME FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE'",
                (config["destination"]["database"],),
            )
            destination_tables = {row[0] for row in cur.fetchall()}
            cur.close()
        except Exception as exc:
            self.messages.put(("log", f"Could not check destination tables for copy marks: {exc}"))
        finally:
            if destination is not None:
                destination.close()
        self.messages.put(("tables", (tables, destination_tables)))

    def _copy(self) -> None:
        if self.copy_resume_state is not None:
            messagebox.showinfo("Resume existing copy", "Resume or finish the saved copy before starting another one.")
            return
        if not self.tables:
            messagebox.showinfo("Inspect source first", "Inspect the source database before copying.")
            return
        if not messagebox.askyesno("Confirm copy", "Copy selected data into the destination? Password hashes may be retained according to the rules."):
            return
        try:
            raw = self.rules_editor.get("1.0", "end-1c").strip()
            rules = json.loads(raw) if raw else load_json(self.rules_path.get())
            if not isinstance(rules, dict):
                raise ValueError("Rules file must contain a JSON object")
            if not rules.get("confirm_copy_password_hashes"):
                raise ValueError("Set confirm_copy_password_hashes=true in the rules after reviewing the credential risk")
            mapping_path = self.mapping_path.get().strip() or None
            if mapping_path is None and not messagebox.askyesno(
                "No mapping store selected",
                "No mapping store is selected. The copy will continue without saving anonymization mappings, "
                "so you will not be able to look up original and anonymized values later. Continue?",
            ):
                return
            selected = [self.table_list.item(item, "values")[1]
                        for item in self.table_list.selection()]
            if not selected:
                messagebox.showinfo("Select tables", "Select at least one table to copy.")
                return
            config = self._config()
            relevant_tables = set(selected)
            if self.include_dependencies.get():
                by_name = {table["name"]: table for table in self.tables}
                pending = list(relevant_tables)
                while pending:
                    current = pending.pop()
                    for dependency in by_name[current].get("references", []):
                        if dependency not in relevant_tables:
                            relevant_tables.add(dependency)
                            pending.append(dependency)
            rules, stale_rules = prune_rules_to_schema(rules, self.tables, relevant_tables)
            if stale_rules:
                shown = stale_rules[:20]
                details = "\n".join(f"  {item}" for item in shown)
                if len(stale_rules) > len(shown):
                    details += f"\n  … and {len(stale_rules) - len(shown)} more"
                if not self._confirm_stale_rules(details):
                    return
            rules_path = None if stale_rules else self.rules_path.get()
            allow_locks = self.allow_locks.get()
            include_dependencies = self.include_dependencies.get()
            state = {
                "version": 1,
                "config": config,
                "rules": rules,
                "selected": selected,
                "mapping_path": mapping_path,
                "rules_path": rules_path,
                "allow_locks": allow_locks,
                "include_dependencies": include_dependencies,
                "completed_counts": {},
            }
            self._save_copy_checkpoint(state)
            self.copy_resume_state = state
            self.pause_event = threading.Event()
            self.copy_in_progress = True
            self.pause_button.configure(text="Pause copy", state="normal")
            self._run_async("Copying selected tables…", self._copy_worker, config, rules, selected,
                            mapping_path, rules_path, allow_locks, include_dependencies,
                            state, self.pause_event)
        except Exception as exc:
            messagebox.showerror("Could not start copy", str(exc))

    def _confirm_stale_rules(self, details: str) -> bool:
        message = (
            "These rules refer to columns missing from the selected source and will be ignored for this copy:\n\n"
            f"{details}\n\nColumns without an applicable rule are copied unchanged. Continue?"
        )
        dialog = tk.Toplevel(self)
        dialog.title("Rules don’t match this source")
        dialog.transient(self)
        dialog.geometry("760x440")
        dialog.minsize(600, 320)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(1, weight=1)
        ttk.Label(dialog, text="Review the rules that will be skipped for this copy:").grid(
            row=0, column=0, sticky="w", padx=12, pady=(12, 6)
        )
        text_frame = ttk.Frame(dialog)
        text_frame.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 8))
        text_frame.rowconfigure(0, weight=1)
        text_frame.columnconfigure(0, weight=1)
        message_text = tk.Text(text_frame, wrap="word", height=14, width=80)
        scrollbar = ttk.Scrollbar(text_frame, orient="vertical", command=message_text.yview)
        message_text.configure(yscrollcommand=scrollbar.set)
        message_text.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        message_text.insert("1.0", message)
        message_text.configure(state="disabled")

        result = {"continue": False}
        buttons = ttk.Frame(dialog, padding=(12, 0, 12, 12))
        buttons.grid(row=2, column=0, sticky="ew")

        def copy_message() -> None:
            self.clipboard_clear()
            self.clipboard_append(message)

        ttk.Button(buttons, text="Copy message", command=copy_message).pack(side="left")

        def finish(continue_copy: bool) -> None:
            result["continue"] = continue_copy
            dialog.destroy()

        ttk.Button(buttons, text="Cancel", command=lambda: finish(False)).pack(side="right")
        ttk.Button(buttons, text="Continue", command=lambda: finish(True)).pack(side="right", padx=(0, 8))
        dialog.bind("<Escape>", lambda _event: finish(False))
        dialog.protocol("WM_DELETE_WINDOW", lambda: finish(False))
        dialog.grab_set()
        message_text.focus_set()
        self.wait_window(dialog)
        return result["continue"]

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
                     mapping_path: str | None, rules_path: str | None, allow_locks: bool,
                     include_dependencies: bool, resume_state: dict[str, Any],
                     pause_event: threading.Event) -> None:
        source = connect_with_guidance("source", config["source"])
        destination = None
        try:
            tables = inspect_schema(source, config["source"]["database"])
            moved = migrate_identifier_rules(rules, tables)
            if moved and rules_path:
                save_json_atomic(rules_path, rules)
            if moved:
                self.messages.put(("log", "Moved identifier columns to preserve: " + ", ".join(moved)))
            destination = connect_with_guidance("destination", config["destination"])
            self.messages.put(("progress", {"event": "copy_started"}))

            def report_progress(progress: dict[str, Any]) -> None:
                if progress.get("event") == "start" and not resume_state.get("is_resume"):
                    # The collision check has passed. From this point, any destination
                    # tables found after an interruption belong to this copy job.
                    resume_state["is_resume"] = True
                    resume_state["selected"] = progress.get("selected_tables", selected)
                    self._save_copy_checkpoint(resume_state)
                elif progress.get("event") == "table_done":
                    completed = dict(resume_state.get("completed_counts", {}))
                    completed[progress["table"]] = progress["copied_rows"]
                    resume_state["completed_counts"] = completed
                    self._save_copy_checkpoint(resume_state)
                self.messages.put(("progress", progress))

            try:
                counts = copy_database(
                    source, destination, config["source"]["database"], rules,
                    config.get("batch_size", 500), allow_locks,
                    mapping_path, selected, include_dependencies,
                    report_progress,
                    resume_state.get("completed_counts", {}), pause_event,
                    bool(resume_state.get("is_resume", False)),
                )
            except CopyPaused as paused:
                resume_state["completed_counts"] = paused.completed_counts
                resume_state["selected"] = paused.selected_tables
                resume_state["is_resume"] = True
                resume_state["paused_table"] = paused.current_table
                self._save_copy_checkpoint(resume_state)
                self.messages.put(("copy_paused", resume_state))
                return
            self._resume_copy_path().unlink(missing_ok=True)
            self.messages.put(("copy_completed", None))
            self.messages.put(("progress", {"event": "copy_finished"}))
            if mapping_path:
                self.messages.put(("lookup_fields", list_mapping_fields(mapping_path)))
            copied = set(counts)
            not_copied = [table["name"] for table in tables if table["name"] not in copied]
            report = ["Copy complete.", "Copied tables:"]
            report.extend(f"  {name}: {count} rows" for name, count in counts.items())
            report.append("Not copied tables:")
            report.extend(f"  {name}" for name in not_copied) if not_copied else report.append("  (none)")
            self.messages.put(("log", "\n".join(report)))
            self.messages.put(("copied_tables", set(counts)))
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
                self.tables, self.copied_table_names = value
                self.table_names = [table["name"] for table in self.tables]
                self.source_inspected = True
                self._render_source_tables()
                self._refresh_rules_preview()
                self._log(f"Found {len(self.tables)} source tables.")
            elif kind == "copied_tables":
                self.copied_table_names.update(value)
                self._render_source_tables()
            elif kind == "log":
                self._log(value)
            elif kind == "progress":
                self._show_progress(value)
            elif kind == "copy_paused":
                self.copy_in_progress = False
                self.copy_resume_state = value
                self.pause_button.configure(text="Resume copy")
                self.status.set("Copy paused and saved. Resume it here or close the app and resume next time.")
            elif kind == "copy_completed":
                self.copy_in_progress = False
                self.copy_resume_state = None
                self.pause_event = None
                self.pause_button.configure(text="Pause copy", state="disabled")
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
                if self.copy_in_progress:
                    self.copy_in_progress = False
                    if self.copy_resume_state is not None and self.copy_resume_state.get("is_resume"):
                        self.pause_button.configure(text="Resume copy")
                    else:
                        try:
                            self._resume_copy_path().unlink(missing_ok=True)
                        except OSError:
                            pass
                        self.copy_resume_state = None
                        self.pause_button.configure(text="Pause copy", state="disabled")
                if self.copy_started_at is not None:
                    elapsed = time.monotonic() - self.copy_started_at
                    self.status.set(f"Operation failed after {self._format_duration(elapsed)}.")
                else:
                    self.status.set("Operation failed.")
                self.copy_started_at = None
                self._log("Error: " + value)
                messagebox.showerror("Operation failed", value)
            elif kind == "done":
                if not self.status.get().startswith(("Showing ", "Copy complete.", "Copy paused")):
                    self.status.set("Ready.")
            if kind in {"error", "done"}:
                self.busy = False
                self._set_busy(False)
                if kind == "done" and self.close_when_paused and not self.copy_in_progress:
                    self.after_idle(self._on_close)
                    return
        self.after(100, self._poll_messages)

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for widget in (self.save_config_button, self.inspect_button, self.test_connections_button,
                       self.copy_button, self.browse_tables_button, self.browse_search_button,
                       self.lookup_fields_button):
            widget.configure(state=state)
        self.copy_button.configure(state="disabled" if self.copy_resume_state is not None else state)
        if self.busy:
            self.pause_button.configure(state="normal" if self.copy_in_progress else "disabled")
        else:
            self.pause_button.configure(
                state="normal" if self.copy_resume_state is not None else "disabled",
                text="Resume copy" if self.copy_resume_state is not None else "Pause copy",
            )

    def _clear_browse_rows(self) -> None:
        self.browse_tree.delete(*self.browse_tree.get_children())
        self.browse_tree.configure(columns=())

    def _render_source_tables(self) -> None:
        selected_names = {self.table_list.item(item, "values")[1]
                          for item in self.table_list.selection()}
        self.table_list.delete(*self.table_list.get_children())
        for index, table in enumerate(self.tables):
            name = table["name"]
            mark = "✓" if name in self.copied_table_names else ""
            count = f"{table['row_count']:,}" if "row_count" in table else "—"
            size = self._format_size(table.get("size_bytes"))
            eta = table.get("copy_eta", "—")
            refs = ", ".join(table.get("references", []))
            item_id = f"table_{index}"
            self.table_list.insert("", "end", iid=item_id,
                                   values=(mark, name, count, size, eta, refs))
            if name in selected_names:
                self.table_list.selection_add(item_id)
            if name in self.copied_table_names:
                self.table_list.item(item_id, tags=("copied",))
        self.table_list.tag_configure("copied", foreground="#167246", background="#f0faf4")
        for index, item_id in enumerate(self.table_list.get_children()):
            if not self.table_list.item(item_id, "tags"):
                self.table_list.item(item_id, tags=("even" if index % 2 == 0 else "odd",))
        self.table_list.tag_configure("even", background="white")
        self.table_list.tag_configure("odd", background="#f7f9fc")

    @staticmethod
    def _format_size(size_bytes: int | None) -> str:
        if size_bytes is None:
            return "size —"
        value = float(size_bytes)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024 or unit == "TB":
                return f"{value:.1f} {unit}"
            value /= 1024
        return "0 B"

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
        if event == "copy_started":
            self.copy_started_at = time.monotonic()
            self.overall_progress.configure(maximum=1, value=0)
            self.table_progress.configure(maximum=1, value=0)
            self.status.set("Preparing copy… Elapsed 00:00:00 · ETA calculating")
            return
        if event == "copy_finished":
            elapsed = time.monotonic() - self.copy_started_at if self.copy_started_at is not None else 0
            self.overall_progress.configure(value=self.overall_progress.cget("maximum"))
            self.table_progress.configure(value=self.table_progress.cget("maximum"))
            self.status.set(f"Copy complete. Time taken: {self._format_duration(elapsed)}.")
            self.copy_started_at = None
            return
        total_rows = progress.get("total_rows", 0)
        total_tables = progress.get("total_tables", 0)
        if event == "start":
            completed_tables = progress.get("completed_tables", 0)
            completed_rows = progress.get("completed_rows", 0)
            self.overall_progress.configure(maximum=max(total_tables, 1), value=completed_tables)
            self.table_progress.configure(maximum=1, value=0)
            self.status.set(self._progress_status(
                f"Preparing {total_tables} table(s), {total_rows} row(s)",
                completed_rows, total_rows, completed_tables, total_tables,
            ))
            return
        table = progress.get("table", "")
        table_index = progress.get("table_index", 0)
        table_rows = progress.get("table_rows", 0)
        copied_rows = progress.get("copied_rows", 0)
        if event in {"table", "batch", "table_done"}:
            elapsed = time.monotonic() - self.copy_started_at if self.copy_started_at is not None else 0
            completed_rows = progress.get("completed_rows", 0)
            rate = completed_rows / elapsed if elapsed > 0 and completed_rows > 0 else 0
            for item in self.tables:
                rows = item.get("row_count")
                if rate > 0 and rows is not None:
                    item["copy_eta"] = self._format_duration(rows / rate)
                else:
                    item["copy_eta"] = "calculating"
            self._render_source_tables()
        if event == "table":
            self.overall_progress.configure(value=max(0, table_index - 1))
            self.table_progress.configure(maximum=max(table_rows, 1), value=0)
        elif event == "batch":
            table_fraction = copied_rows / table_rows if table_rows else 0
            self.overall_progress.configure(value=max(0, table_index - 1 + table_fraction))
            self.table_progress.configure(maximum=max(table_rows, 1), value=copied_rows)
        elif event == "table_done":
            self.overall_progress.configure(value=progress.get("completed_tables", table_index))
            self.table_progress.configure(maximum=max(table_rows, 1), value=max(table_rows, 1))
        self.status.set(self._progress_status(
            f"Copying table {table_index}/{total_tables}: {table} "
            f"({copied_rows}/{table_rows} rows; {progress.get('completed_rows', 0)}/{total_rows} total)",
            progress.get("completed_rows", 0), total_rows,
            progress.get("completed_tables", table_index - 1), total_tables,
        ))

    def _progress_status(self, description: str, completed_rows: int, total_rows: int,
                         completed_tables: int, total_tables: int) -> str:
        elapsed = time.monotonic() - self.copy_started_at if self.copy_started_at is not None else 0
        if total_rows:
            completed, total = completed_rows, total_rows
        else:
            completed, total = completed_tables, total_tables
        if completed > 0 and total > completed:
            eta = self._format_duration(elapsed * (total - completed) / completed)
        elif completed >= total and total > 0:
            eta = "finishing"
        else:
            eta = "calculating"
        return f"{description} · Elapsed {self._format_duration(elapsed)} · ETA {eta}"

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total_seconds = max(0, int(seconds))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours >= 24:
            days, hours = divmod(hours, 24)
            return f"{days}d {hours:02}:{minutes:02}:{secs:02}"
        return f"{hours:02}:{minutes:02}:{secs:02}"

    def _select_all(self) -> None:
        self.table_list.selection_set(self.table_list.get_children())

    def _clear_selection(self) -> None:
        self.table_list.selection_remove(self.table_list.selection())

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
