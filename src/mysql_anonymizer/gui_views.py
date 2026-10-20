"""Tk widget construction and presentation helpers for the desktop app."""

from __future__ import annotations

import json
import tkinter as tk
from tkinter import ttk
from typing import Any


class ViewMixin:
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
        self.overall_progress_title = tk.StringVar(value="Overall progress")
        ttk.Label(progress_panel, textvariable=self.overall_progress_title, style="Heading.TLabel").pack(anchor="w")
        self.overall_progress = ttk.Progressbar(progress_panel, mode="determinate", maximum=1, value=0)
        self.overall_progress.pack(fill="x", pady=(0, 3))
        self.table_progress_title = tk.StringVar(value="Current table")
        ttk.Label(progress_panel, textvariable=self.table_progress_title, style="Heading.TLabel").pack(anchor="w")
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

    def _browse_update_columns(self) -> None:
        role = self.browse_role.get()
        table_name = self.browse_table.get()
        table = next((item for item in self.browse_tables_by_role.get(role, [])
                      if item["name"] == table_name), None)
        columns = [column["COLUMN_NAME"] for column in table["columns"]] if table else []
        self.browse_column_box.configure(values=[""] + columns)
        self.browse_column.set("")

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
            row_count = table.get("row_count")
            count = f"{row_count:,}" if row_count is not None else "—"
            if count != "—" and table.get("row_count_is_estimate"):
                count = f"~{count}"
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

    def _select_all(self) -> None:
        self.table_list.selection_set(self.table_list.get_children())

    def _clear_selection(self) -> None:
        self.table_list.selection_remove(self.table_list.selection())
