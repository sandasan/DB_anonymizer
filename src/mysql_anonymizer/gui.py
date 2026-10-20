from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any
from logging.handlers import RotatingFileHandler

from .cli import check_config
from .gui_settings import SettingsMixin
from .gui_views import ViewMixin
from .core import (
    CopyPaused,
    clear_destination_database_data,
    connect,
    copy_database,
    drop_destination_tables,
    inspect_schema,
    list_mapping_fields,
    load_json,
    lookup_mapping,
    migrate_identifier_rules,
    prune_rules_to_schema,
    recreate_destination_database,
    resolve_table_selection,
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


class AnonymizerApp(SettingsMixin, ViewMixin, tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("MySQL Anonymized Copy")
        self.geometry("1120x820")
        self.minsize(900, 680)
        self._configure_style()
        self.tables: list[dict[str, Any]] = []
        self.table_names: list[str] = []
        self.copied_table_names: set[str] = set()
        self.recreated_table_names: set[str] = set()
        self.recreated_destination_identity: tuple[str, str, str] | None = None
        self.recreated_tables_by_destination: dict[str, set[str]] = {}
        self.saved_auto_confirm_operations = False
        self.source_inspected = False
        self.messages: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.busy = False
        self.copy_started_at: float | None = None
        self.pause_event: threading.Event | None = None
        self.copy_in_progress = False
        self.inspection_in_progress = False
        self.inspection_snapshot: dict[str, Any] | None = None
        self.inspection_stale = False
        self.copy_schema_verified = False
        self.copy_after_schema_check = False
        self.pending_copy_action: str | None = None
        self.close_when_paused = False
        self.copy_resume_state: dict[str, Any] | None = None

        self.config_path = tk.StringVar(value="config.json")
        self.rules_path = tk.StringVar(value="rules.json")
        self.mapping_path = tk.StringVar()
        self._init_diagnostics()
        self._load_last_paths()
        self.auto_confirm_operations = tk.BooleanVar(value=self.saved_auto_confirm_operations)
        self.allow_locks = tk.BooleanVar(value=False)
        self.include_dependencies = tk.BooleanVar(value=True)
        self.saved_table_selection: list[str] = []
        self.connection_fields: dict[str, dict[str, tk.StringVar]] = {}
        self.password_fields: dict[str, tk.StringVar] = {}
        self._build_ui()
        self._log(f"Diagnostic log: {self.diagnostic_log_path}")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_messages)
        self.after(250, self._offer_resume_copy)

    @staticmethod
    def _state_file() -> Path:
        if os.name == "nt":
            config_root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        elif sys.platform == "darwin":
            config_root = Path.home() / "Library" / "Application Support"
        else:
            config_root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        return config_root / "mysql-anonymized-copy" / "gui_state.json"

    def _init_diagnostics(self) -> None:
        self.diagnostic_log_path = self._state_file().with_name("gui.log")
        self.diagnostic_logger = logging.getLogger(f"mysql_anonymized_copy.gui.{id(self)}")
        self.diagnostic_logger.setLevel(logging.INFO)
        self.diagnostic_logger.propagate = False
        try:
            self.diagnostic_log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                self.diagnostic_log_path.parent.chmod(0o700)
            except OSError:
                pass
            handler = RotatingFileHandler(
                self.diagnostic_log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
            )
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            self.diagnostic_logger.addHandler(handler)
            try:
                self.diagnostic_log_path.chmod(0o600)
            except OSError:
                pass
            self._diagnostic("application_started")
        except OSError:
            self.diagnostic_logger.addHandler(logging.NullHandler())

    @staticmethod
    def _diagnostic_error(exc: Exception) -> str:
        message = str(exc)
        message = re.sub(r"(?i)(password|passwd|pwd)\s*[:=]\s*([^\s,;]+)", r"\1=<redacted>", message)
        message = re.sub(r"(?i)primary key\s+\{[^}]*\}", "primary key <redacted>", message)
        message = re.sub(r"(?i)\bvalue\s+(?:'[^']*'|\"[^\"]*\")", "value <redacted>", message)
        return f"{type(exc).__name__}: {message[:1200]}"

    def _diagnostic(self, event: str, detail: str = "") -> None:
        self.diagnostic_logger.info("%s%s", event, f": {detail}" if detail else "")
        try:
            self.diagnostic_log_path.chmod(0o600)
        except OSError:
            pass

    def _load_last_paths(self) -> None:
        try:
            state = json.loads(self._state_file().read_text(encoding="utf-8"))
            config_path = state.get("config_path")
            rules_path = state.get("rules_path")
            if isinstance(config_path, str) and config_path:
                self.config_path.set(config_path)
            if isinstance(rules_path, str) and rules_path:
                self.rules_path.set(rules_path)
            mapping_path = state.get("mapping_path")
            if isinstance(mapping_path, str):
                self.mapping_path.set(mapping_path)
            self.saved_auto_confirm_operations = bool(state.get("auto_confirm_operations", False))
            snapshot = state.get("inspection_snapshot")
            if isinstance(snapshot, dict):
                self.inspection_snapshot = snapshot
            recreated = state.get("recreated_tables_by_destination", {})
            if isinstance(recreated, dict):
                self.recreated_tables_by_destination = {
                    key: {name for name in names if isinstance(name, str)}
                    for key, names in recreated.items()
                    if isinstance(key, str) and isinstance(names, list)
                }
        except (OSError, json.JSONDecodeError, AttributeError):
            pass

    def _save_last_paths(self) -> None:
        path = self._state_file()
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(state, dict):
                    state = {}
            except (OSError, json.JSONDecodeError):
                state = {}
            state.update({
                "config_path": self.config_path.get(),
                "rules_path": self.rules_path.get(),
                "mapping_path": self.mapping_path.get(),
                "auto_confirm_operations": self.auto_confirm_operations.get(),
            })
            if self.inspection_snapshot is not None:
                state["inspection_snapshot"] = self.inspection_snapshot
            state["recreated_tables_by_destination"] = {
                key: sorted(names)
                for key, names in self.recreated_tables_by_destination.items()
                if names
            }
            path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
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

















    def _inspect(self) -> None:
        try:
            config = self._config()
            self.inspection_stale = False
            self.copy_schema_verified = False
            self.overall_progress.configure(mode="indeterminate", maximum=1, value=0)
            self.overall_progress.start(12)
            self.table_progress.configure(mode="indeterminate", maximum=1, value=0)
            self.table_progress.start(12)
            self.status.set("Connecting to source and reading schema…")
            self.inspection_in_progress = True
            self._run_async("Inspecting source schema…", self._inspect_worker, config)
        except Exception as exc:
            self.inspection_in_progress = False
            self._reset_inspection_progress()
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
            self.messages.put(("inspect_progress", {"phase": "schema"}))
            tables = inspect_schema(source, config["source"]["database"])
            self.messages.put(("inspect_progress", {"phase": "counts_start", "total": len(tables)}))
            cursor = source.cursor()
            cursor.execute(
                "SELECT TABLE_NAME, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH "
                "FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE'",
                (config["source"]["database"],),
            )
            table_stats = {
                name: {
                    "row_count": row_count,
                    "size_bytes": (data_length or 0) + (index_length or 0),
                }
                for name, row_count, data_length, index_length in cursor.fetchall()
            }
            for index, table in enumerate(tables, start=1):
                self.messages.put(("inspect_progress", {
                    "phase": "estimate_start", "table": table["name"], "index": index,
                    "total": len(tables),
                }))
                stats = table_stats.get(table["name"], {})
                table["row_count"] = stats.get("row_count")
                table["row_count_is_estimate"] = str(table.get("engine") or "").lower() == "innodb"
                table["size_bytes"] = stats.get("size_bytes", 0)
                self.messages.put(("inspect_progress", {
                    "phase": "estimate_done", "table": table["name"], "index": index,
                    "total": len(tables),
                }))
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
        self.messages.put(("tables", (tables, destination_tables, config)))

    @staticmethod
    def _source_identity(config: dict[str, Any]) -> dict[str, Any]:
        source = config["source"]
        return {key: source.get(key) for key in ("host", "port", "database", "user")}

    @staticmethod
    def _destination_identity(config: dict[str, Any]) -> tuple[str, str, str]:
        destination = config["destination"]
        return (str(destination.get("host", "")).casefold(),
                str(destination.get("port", "3306")),
                str(destination.get("database", "")).casefold())

    @staticmethod
    def _destination_identity_key(identity: tuple[str, str, str]) -> str:
        return json.dumps(identity, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _schema_fingerprint(tables: list[dict[str, Any]]) -> str:
        schema = [{
            "name": table["name"],
            "engine": table.get("engine"),
            "references": sorted(table.get("references", [])),
            "external_references": sorted(table.get("external_references", [])),
            "columns": [{key: column.get(key) for key in (
                "COLUMN_NAME", "DATA_TYPE", "COLUMN_TYPE", "IS_NULLABLE", "COLUMN_KEY", "EXTRA"
            )} for column in table.get("columns", [])],
        } for table in tables]
        encoded = json.dumps(schema, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _copy_schema_check_worker(self, config: dict[str, Any], snapshot: dict[str, Any]) -> None:
        source = connect_with_guidance("source", config["source"])
        try:
            tables = inspect_schema(source, config["source"]["database"])
        finally:
            source.close()
        unchanged = (
            snapshot.get("source") == self._source_identity(config)
            and snapshot.get("fingerprint") == self._schema_fingerprint(tables)
        )
        self.messages.put(("copy_schema_check", unchanged))

    def _recreate_selected_tables(self) -> None:
        selected = self._selected_table_names()
        if not selected:
            messagebox.showinfo("Select tables", "Select the source tables to recreate in the destination.")
            return
        pending_state = self.copy_resume_state
        unfinished: list[str] = []
        if pending_state is not None:
            pending_tables = pending_state.get("selected", [])
            completed_tables = pending_state.get("completed_counts", {})
            if isinstance(pending_tables, list) and isinstance(completed_tables, dict):
                unfinished = [name for name in pending_tables
                              if isinstance(name, str) and name not in completed_tables]
            selected = list(dict.fromkeys([*selected, *unfinished]))
        destination = self.connection_fields["destination"]["database"].get().strip()
        names = "\n".join(f"  {name}" for name in selected)
        if pending_state is not None:
            pending_names = "\n".join(f"  {name}" for name in unfinished) or "  (none)"
            prompt = (
                "A previous copy is paused or interrupted. Continuing will discard its resume checkpoint. "
                "Any unfinished tables from that copy will be added to this recreation, dropped, and copied again. "
                "Completed tables from the previous copy will be kept unless selected below.\n\n"
                f"Unfinished tables to include:\n{pending_names}\n\n"
                f"Tables to recreate:\n{names}\n\n"
                f"Destination database: {destination}\n\nContinue and discard the saved checkpoint?"
            )
            title = "Discard previous copy and recreate tables"
        else:
            prompt = (
                "This will permanently drop these destination tables, then recreate and recopy them from the source:\n\n"
                f"{names}\n\nDestination database: {destination}\n\nContinue?"
            )
            title = "Recreate destination tables"
        if pending_state is not None or not self.auto_confirm_operations.get():
            if not messagebox.askyesno(title, prompt):
                return
        if pending_state is not None:
            try:
                self._resume_copy_path().unlink(missing_ok=True)
            except OSError as exc:
                messagebox.showerror("Could not discard saved copy", str(exc))
                return
            self.copy_resume_state = None
            self.pause_event = None
            self._table_selected_names.update(unfinished)
            self._render_source_tables()
        self._log(f"Starting destination table recreation for {len(selected)} selected table(s).")
        self._copy(action="replace_tables")

    def _recreate_destination_database(self) -> None:
        if self.copy_resume_state is not None:
            messagebox.showinfo("Resume existing copy", "Resume or finish the saved copy before recreating the database.")
            return
        if not self.tables:
            messagebox.showinfo("Inspect source first", "Inspect the source before recreating the destination database.")
            return
        destination = self.connection_fields["destination"]["database"].get().strip()
        if not destination:
            messagebox.showerror("Destination database required", "Enter the destination database name first.")
            return
        typed_name = simpledialog.askstring(
            "Recreate destination database",
            f"This permanently deletes the entire destination database and all its contents, then recreates it and copies all source tables.\n\n"
            f"Type the destination database name to continue:\n{destination}",
            parent=self,
        )
        if typed_name != destination:
            return
        self._copy(action="recreate_database")

    def _clear_destination_data(self) -> None:
        if self.copy_in_progress or self.busy:
            return
        destination = self.connection_fields["destination"]["database"].get().strip()
        if not destination:
            messagebox.showerror("Destination database required", "Enter the destination database name first.")
            return
        typed_name = simpledialog.askstring(
            "Clear destination data",
            "This permanently drops every base table in the destination database and discards any saved copy checkpoint. The database itself and its settings remain.\n\n"
            f"Type the destination database name to continue:\n{destination}",
            parent=self,
        )
        if typed_name != destination:
            return
        try:
            config = self._config()
        except Exception as exc:
            messagebox.showerror("Invalid connection settings", str(exc))
            return
        try:
            self._resume_copy_path().unlink(missing_ok=True)
        except OSError as exc:
            messagebox.showerror("Could not discard saved copy", str(exc))
            return
        self.copy_resume_state = None
        self.pause_event = None
        self._run_async("Clearing destination data…", self._clear_destination_worker, config)

    def _clear_destination_worker(self, config: dict[str, Any]) -> None:
        names = clear_destination_database_data(config["destination"], config["source"])
        identity = self._destination_identity(config)
        self._diagnostic("destination_data_cleared", f"table_count={len(names)}")
        self.messages.put(("destination_data_cleared", (identity, names)))

    def _copy(self, action: str = "copy") -> None:
        if self.copy_resume_state is not None:
            messagebox.showinfo("Resume existing copy", "Resume or finish the saved copy before starting another one.")
            return
        if not self.tables:
            messagebox.showinfo("Inspect source first", "Inspect the source database before copying.")
            return
        if self.inspection_stale:
            messagebox.showinfo("Inspect source again", "The source schema changed since its last inspection. Inspect the source again before copying.")
            return
        if not self.copy_schema_verified:
            if self.inspection_snapshot is None:
                messagebox.showinfo("Inspect source first", "Inspect the source before copying so the app can check for schema changes.")
                return
            try:
                config = self._config()
            except Exception as exc:
                messagebox.showerror("Invalid connection settings", str(exc))
                return
            self.copy_after_schema_check = True
            self.pending_copy_action = action
            self.overall_progress_title.set("Checking source schema")
            self.table_progress_title.set("Checking source schema")
            self.overall_progress.configure(mode="indeterminate", maximum=1, value=0)
            self.table_progress.configure(mode="indeterminate", maximum=1, value=0)
            self.overall_progress.start(12)
            self.table_progress.start(12)
            self._run_async("Checking whether the source schema changed…", self._copy_schema_check_worker,
                            config, self.inspection_snapshot)
            return
        self.copy_schema_verified = False
        if (action == "copy" and not self.auto_confirm_operations.get()
                and not messagebox.askyesno("Confirm copy", "Copy selected data into the destination? Password hashes may be retained according to the rules.")):
            return
        try:
            raw = self.rules_editor.get("1.0", "end-1c").strip()
            rules = json.loads(raw) if raw else load_json(self.rules_path.get())
            if not isinstance(rules, dict):
                raise ValueError("Rules file must contain a JSON object")
            if not rules.get("confirm_copy_password_hashes"):
                raise ValueError("Set confirm_copy_password_hashes=true in the rules after reviewing the credential risk")
            mapping_path = self.mapping_path.get().strip() or None
            if mapping_path is None and not self.auto_confirm_operations.get() and not messagebox.askyesno(
                "No mapping store selected",
                "No mapping store is selected. The copy will continue without saving anonymization mappings, "
                "so you will not be able to look up original and anonymized values later. Continue?",
            ):
                return
            selected = self._selected_table_names()
            if action == "recreate_database":
                selected = [table["name"] for table in self.tables]
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
                if not self.auto_confirm_operations.get() and not self._confirm_stale_rules(details):
                    return
            rules_path = None if stale_rules else self.rules_path.get()
            allow_locks = self.allow_locks.get()
            include_dependencies = self.include_dependencies.get() or action == "recreate_database"
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
                "recreate_action": action,
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
            self.pending_copy_action = None
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
            action = resume_state.get("recreate_action", "copy")
            if action == "recreate_database" and not resume_state.get("is_resume"):
                self._diagnostic("destination_database_recreation_started")
                recreate_destination_database(config["destination"], config["source"])
                self._diagnostic("destination_database_recreated")
            destination = connect_with_guidance("destination", config["destination"])
            if action == "replace_tables" and not resume_state.get("is_resume"):
                check = destination.cursor()
                try:
                    check.execute("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()")
                    existing_tables = {row[0] for row in check.fetchall()}
                finally:
                    check.close()
                resolve_table_selection(
                    tables,
                    selected,
                    include_dependencies,
                    existing_tables - set(selected),
                )
                self._diagnostic("destination_table_drop_started", f"table_count={len(selected)}")
                self.messages.put(("operation_status", f"Dropping {len(selected)} destination table(s)…"))
                drop_destination_tables(destination, selected)
                self._diagnostic("destination_table_drop_completed", f"table_count={len(selected)}")
                self.messages.put(("log", "Destination tables dropped; recreating from source…"))
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
                    self.messages.put((
                        "table_copied",
                        (progress["table"], action, self._destination_identity(config)),
                    ))
                if progress.get("event") in {"start", "table", "table_done"}:
                    self._diagnostic(
                        "copy_progress",
                        f"event={progress.get('event')} table={progress.get('table', '')!r} "
                        f"completed_tables={progress.get('completed_tables', '')} "
                        f"total_tables={progress.get('total_tables', '')} "
                        f"copied_rows={progress.get('copied_rows', '')}",
                    )
                self.messages.put(("progress", progress))

            try:
                counts = copy_database(
                    source, destination, config["source"]["database"], rules,
                    config.get("batch_size", 2000), allow_locks,
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
            self.messages.put(("copied_tables", (set(counts), action, self._destination_identity(config))))
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
        self._diagnostic("operation_started", status)

        def run() -> None:
            try:
                operation(*args)
                self._diagnostic("operation_worker_completed", status)
                self.messages.put(("done", None))
            except Exception as exc:
                self._diagnostic("operation_failed", self._diagnostic_error(exc))
                self.messages.put(("error", str(exc)))

        threading.Thread(target=run, daemon=True).start()

    def _poll_messages(self) -> None:
        while True:
            try:
                kind, value = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == "tables":
                self.tables, self.copied_table_names, config = value
                self._diagnostic("inspection_completed", f"source_tables={len(self.tables)}")
                destination_identity = self._destination_identity(config)
                identity_key = self._destination_identity_key(destination_identity)
                self.recreated_destination_identity = destination_identity
                self.recreated_table_names = (
                    self.recreated_tables_by_destination.get(identity_key, set())
                    & self.copied_table_names
                )
                self.recreated_tables_by_destination[identity_key] = set(self.recreated_table_names)
                self.table_names = [table["name"] for table in self.tables]
                self.source_inspected = True
                self.inspection_stale = False
                self.copy_schema_verified = False
                self.inspection_snapshot = {
                    "source": self._source_identity(config),
                    "fingerprint": self._schema_fingerprint(self.tables),
                }
                self._save_last_paths()
                self._render_source_tables()
                self._restore_table_selection()
                self._refresh_rules_preview()
                self._log(f"Found {len(self.tables)} source tables.")
                self._reset_inspection_progress()
                self.inspection_in_progress = False
                self.status.set(f"Inspection complete. Found {len(self.tables)} source table(s).")
            elif kind == "inspect_progress":
                self._show_inspection_progress(value)
            elif kind == "copy_schema_check":
                self._reset_inspection_progress()
                if value:
                    self.copy_schema_verified = True
                    self.status.set("Source schema is unchanged. Continuing with copy setup…")
                else:
                    self.inspection_stale = True
                    self.copy_schema_verified = False
                    self.status.set("Source schema changed. Inspect the source again before copying.")
                    messagebox.showinfo(
                        "Source schema changed",
                        "The source database or its table structure changed since the last inspection. "
                        "Run Inspect source again before copying.",
                    )
            elif kind == "copied_tables":
                copied_names, action, destination_identity = value
                self.copied_table_names.update(copied_names)
                if action in {"replace_tables", "recreate_database"}:
                    identity_key = self._destination_identity_key(destination_identity)
                    if self.recreated_destination_identity != destination_identity:
                        self.recreated_table_names = set(
                            self.recreated_tables_by_destination.get(identity_key, set())
                        )
                    self.recreated_destination_identity = destination_identity
                    self.recreated_table_names.update(copied_names)
                    self.recreated_tables_by_destination[identity_key] = set(self.recreated_table_names)
                self._table_selected_names.difference_update(copied_names)
                self._render_source_tables()
                self._save_last_paths()
            elif kind == "table_copied":
                table_name, action, destination_identity = value
                self.copied_table_names.add(table_name)
                if action in {"replace_tables", "recreate_database"}:
                    identity_key = self._destination_identity_key(destination_identity)
                    if self.recreated_destination_identity != destination_identity:
                        self.recreated_table_names = set(
                            self.recreated_tables_by_destination.get(identity_key, set())
                        )
                    self.recreated_destination_identity = destination_identity
                    self.recreated_table_names.add(table_name)
                    self.recreated_tables_by_destination[identity_key] = set(self.recreated_table_names)
                self._render_source_tables()
                self._save_last_paths()
            elif kind == "destination_data_cleared":
                identity, names = value
                self.copied_table_names.clear()
                identity_key = self._destination_identity_key(identity)
                self.recreated_tables_by_destination.pop(identity_key, None)
                if self.recreated_destination_identity == identity:
                    self.recreated_table_names.clear()
                self._render_source_tables()
                self._save_last_paths()
                self.messages.put(("log", f"Cleared {len(names)} destination table(s) and reset copy state."))
                self.messages.put(("operation_status", "Destination data cleared; copy state reset."))
            elif kind == "log":
                self._log(value)
            elif kind == "operation_status":
                self.status.set(value)
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
                self._diagnostic("operation_error_displayed", self._diagnostic_error(RuntimeError(value)))
                self.copy_after_schema_check = False
                if self.copy_schema_verified is False and self.status.get().startswith("Checking whether"):
                    self._reset_inspection_progress()
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
                if self.inspection_in_progress:
                    self._reset_inspection_progress()
                    self.inspection_in_progress = False
                self.pending_copy_action = None
                self.copy_started_at = None
                self._log("Error: " + value)
                messagebox.showerror("Operation failed", value)
            elif kind == "done":
                if not self.status.get().startswith(("Showing ", "Copy complete.", "Copy paused", "Inspection complete")):
                    self.status.set("Ready.")
            if kind in {"error", "done"}:
                self.busy = False
                self._set_busy(False)
                if kind == "done" and self.copy_after_schema_check:
                    self.copy_after_schema_check = False
                    if self.copy_schema_verified:
                        action = self.pending_copy_action or "copy"
                        self.pending_copy_action = None
                        self.after_idle(lambda: self._copy(action))
                if kind == "done" and self.close_when_paused and not self.copy_in_progress:
                    self.after_idle(self._on_close)
                    return
        self.after(100, self._poll_messages)

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for widget in (self.save_config_button, self.inspect_button, self.test_connections_button,
                       self.copy_button, self.browse_tables_button, self.browse_search_button,
                       self.lookup_fields_button, self.recreate_tables_button,
                       self.recreate_database_button, self.clear_destination_button):
            widget.configure(state=state)
        self.copy_button.configure(state="disabled" if self.copy_resume_state is not None else state)
        if self.busy:
            self.pause_button.configure(state="normal" if self.copy_in_progress else "disabled")
        else:
            self.pause_button.configure(
                state="normal" if self.copy_resume_state is not None else "disabled",
                text="Resume copy" if self.copy_resume_state is not None else "Pause copy",
            )

    def _show_inspection_progress(self, progress: dict[str, Any]) -> None:
        phase = progress.get("phase")
        self.overall_progress_title.set("Overall inspection progress")
        self.table_progress_title.set("Current table inspection")
        if phase in {"schema", "counts_start"}:
            self.overall_progress.stop()
            self.table_progress.stop()
        if phase == "schema":
            self.overall_progress.configure(mode="indeterminate", maximum=1, value=0)
            self.overall_progress.start(12)
            self.table_progress.configure(mode="determinate", maximum=1, value=0)
            self.status.set("Reading source tables, columns, and foreign keys…")
        elif phase == "counts_start":
            total = progress.get("total", 0)
            self.overall_progress.configure(mode="determinate", maximum=max(total, 1), value=0)
            self.table_progress.configure(mode="indeterminate", maximum=1, value=0)
            if total:
                self.table_progress.start(12)
            self.status.set(f"Reading row estimates for {total} table(s)…")
        elif phase == "estimate_start":
            self.overall_progress.configure(value=max(0, progress["index"] - 1))
            self.table_progress.configure(mode="indeterminate", maximum=1, value=0)
            self.table_progress.start(12)
            self.status.set(
                f"Reading row estimate for table {progress['index']}/{progress['total']}: {progress['table']}…"
            )
        elif phase == "estimate_done":
            self.table_progress.stop()
            self.table_progress.configure(mode="determinate", maximum=1, value=1)
            self.overall_progress.configure(value=progress["index"])
            self.status.set(
                f"Read row estimate for table {progress['index']}/{progress['total']}: {progress['table']}"
            )

    def _reset_inspection_progress(self) -> None:
        self.overall_progress.stop()
        self.table_progress.stop()
        self.overall_progress.configure(mode="determinate", maximum=1, value=0)
        self.table_progress.configure(mode="determinate", maximum=1, value=0)
        self.overall_progress_title.set("Overall progress")
        self.table_progress_title.set("Current table")

    def _show_progress(self, progress: dict[str, Any]) -> None:
        event = progress["event"]
        if event == "copy_started":
            self.overall_progress_title.set("Overall copy progress")
            self.table_progress_title.set("Current table copy progress")
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
                f"Preparing copy · {total_tables} table(s)",
                completed_rows, total_rows, completed_tables, total_tables,
            ))
            return
        table_name = progress.get("table", "")
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
            # Progress arrives for every batch. Rebuilding the full table view here
            # makes large inspections/copies appear to freeze; update visible ETA
            # cells in place and leave ordering to the next explicit sort action.
            for item_id in self.table_list.get_children():
                try:
                    source_index = int(item_id.removeprefix("table_"))
                    view_table = self.tables[source_index]
                except (ValueError, IndexError):
                    continue
                values = list(self.table_list.item(item_id, "values"))
                if len(values) > 4:
                    values[4] = view_table.get("copy_eta", "—")
                    self.table_list.item(item_id, values=values)
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
            f"Copying table {table_index}/{total_tables}: {table_name}",
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
