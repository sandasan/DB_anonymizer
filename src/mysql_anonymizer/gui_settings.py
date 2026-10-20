"""Configuration, rule, and mapping-store workflows for the desktop app."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog
from typing import Any
from urllib.parse import quote

from .cli import check_config
from .core import init_rules, list_mapping_fields, load_json, open_mapping_store, save_json_atomic


class SettingsMixin:
    def _profiles_directory(self) -> Path:
        return self._state_file().parent / "profiles"

    def _profile_path(self, name: str) -> Path:
        return self._profiles_directory() / f"{quote(name, safe='')}.json"

    def _refresh_profiles(self) -> None:
        directory = self._profiles_directory()
        names = []
        try:
            for path in directory.glob("*.json"):
                try:
                    profile = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(profile, dict) and isinstance(profile.get("name"), str):
                    names.append(profile["name"])
        except OSError:
            pass
        names.sort(key=str.casefold)
        self.profile_box.configure(values=names)
        if self.profile_selection.get() not in names:
            self.profile_selection.set(names[0] if names else "")

    def _save_profile_as(self) -> None:
        name = simpledialog.askstring("Save app profile", "Profile name:", parent=self)
        if name is None:
            return
        name = name.strip()
        if not name:
            messagebox.showerror("Profile name required", "Enter a name for this profile.")
            return
        path = self._profile_path(name)
        if path.exists() and not messagebox.askyesno(
            "Replace profile", f"A profile named {name!r} already exists. Replace it?", parent=self
        ):
            return
        try:
            config = self._config()
            raw_rules = self.rules_editor.get("1.0", "end-1c").strip()
            rules = json.loads(raw_rules) if raw_rules else load_json(self.rules_path.get())
            if not isinstance(rules, dict):
                raise ValueError("Rules must be a JSON object")
            directory = self._profiles_directory()
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                directory.chmod(0o700)
            except OSError:
                pass
            profile = {
                "version": 1,
                "name": name,
                "config": config,
                "rules": rules,
                "config_path": self.config_path.get(),
                "rules_path": self.rules_path.get(),
                "mapping_path": self.mapping_path.get(),
                "auto_confirm_operations": self.auto_confirm_operations.get(),
                "show_selected_tables_on_top": self.selected_tables_on_top.get(),
            }
            save_json_atomic(str(path), profile)
            try:
                path.chmod(0o600)
            except OSError:
                pass
            self.profile_selection.set(name)
            self._refresh_profiles()
            self._log(f"Saved app profile {name!r}.")
        except Exception as exc:
            messagebox.showerror("Could not save profile", str(exc))

    def _load_profile(self) -> None:
        name = self.profile_selection.get().strip()
        if not name:
            messagebox.showinfo("Select profile", "Choose a profile to load.")
            return
        try:
            profile = load_json(str(self._profile_path(name)))
            if profile.get("version") != 1 or profile.get("name") != name:
                raise ValueError("Unsupported or invalid profile file")
            config = profile.get("config")
            rules = profile.get("rules")
            if not isinstance(config, dict) or not isinstance(rules, dict):
                raise ValueError("Profile must contain config and rules objects")
            check_config(config)
            self.config_path.set(str(profile.get("config_path") or "config.json"))
            self.rules_path.set(str(profile.get("rules_path") or "rules.json"))
            self.mapping_path.set(str(profile.get("mapping_path") or ""))
            self._apply_config(config, profile=True)
            self._set_rules_text(json.dumps(rules, indent=2, ensure_ascii=False))
            self.auto_confirm_operations.set(bool(profile.get("auto_confirm_operations", False)))
            self.selected_tables_on_top.set(bool(profile.get("show_selected_tables_on_top", False)))
            if self.tables:
                self._restore_table_selection()
                self._render_source_tables()
            self._save_last_paths()
            self._log(f"Loaded app profile {name!r}.")
        except Exception as exc:
            messagebox.showerror("Could not load profile", str(exc))

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
                self._save_last_paths()
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

    def _filter_lookup_tables(self) -> None:
        query = self.lookup_table_filter.get().strip().casefold()
        tables = [name for name in self.lookup_fields_by_table if query in name.casefold()]
        self.lookup_table_box.configure(values=tables)
        if self.lookup_table.get() not in tables:
            self.lookup_table.set(tables[0] if tables else "")
            self._filter_lookup_columns()

    def _lookup_table_selected(self, _event=None) -> None:
        self._filter_lookup_columns()

    def _filter_lookup_columns(self) -> None:
        query = self.lookup_column_filter.get().strip().casefold()
        columns = [
            name for name in self.lookup_fields_by_table.get(self.lookup_table.get(), [])
            if query in name.casefold()
        ]
        self.lookup_column_box.configure(values=columns)
        if self.lookup_column.get() not in columns:
            self.lookup_column.set(columns[0] if columns else "")

    def _reset_lookup_filters(self) -> None:
        self.lookup_table_filter.set("")
        self.lookup_column_filter.set("")
        self.lookup_value.set("")
        self._filter_lookup_tables()
        self._filter_lookup_columns()

    def _load_config(self) -> None:
        try:
            config = load_json(self.config_path.get())
            self._apply_config(config)
            self._save_last_paths()
            self._log(f"Loaded config from {self.config_path.get()}.")
        except Exception as exc:
            messagebox.showerror("Could not load config", str(exc))

    def _apply_config(self, config: dict[str, Any], profile: bool = False) -> None:
        self.profile_config = copy.deepcopy(config) if profile else None
        for role in ("source", "destination"):
            settings = config.get(role, {})
            for key, var in self.connection_fields[role].items():
                var.set(str(settings.get(key, "127.0.0.1" if key == "host" else "3306" if key == "port" else "")))
            self.password_fields[role].set(str(settings.get("password", "")))
        self.allow_locks.set(bool(config.get("allow_source_table_locks", False)))
        tables_view = config.get("tables_view", {})
        if not isinstance(tables_view, dict):
            tables_view = {}
        self.include_dependencies.set(bool(tables_view.get("include_dependencies", True)))
        selection = tables_view.get("selected_tables", [])
        self.saved_table_selection = [name for name in selection if isinstance(name, str)] if isinstance(selection, list) else []
        if self.tables:
            self._restore_table_selection()

    def _config(self) -> dict[str, Any]:
        if self.profile_config is not None:
            config = copy.deepcopy(self.profile_config)
        else:
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
        tables_view = config.get("tables_view", {})
        if not isinstance(tables_view, dict):
            tables_view = {}
        else:
            tables_view = dict(tables_view)
        selected_tables = self._selected_table_names() if self.tables else self.saved_table_selection
        tables_view.update({
            "include_dependencies": self.include_dependencies.get(),
            "selected_tables": selected_tables,
        })
        config["tables_view"] = tables_view
        check_config(config)
        return config

    def _restore_table_selection(self) -> None:
        available = set(self.table_names)
        wanted = set(self.saved_table_selection) & available
        self._table_selected_names = set(wanted)
        self.table_list.selection_remove(self.table_list.selection())
        for item_id in self.table_list.get_children():
            if self.table_list.item(item_id, "values")[1] in wanted:
                self.table_list.selection_add(item_id)

    def _save_config(self) -> None:
        try:
            config = self._config()
            with open(self.config_path.get(), "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)
                f.write("\n")
            self._log(f"Saved connection settings and entered passwords to {self.config_path.get()}.")
            self.profile_config = copy.deepcopy(config)
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
