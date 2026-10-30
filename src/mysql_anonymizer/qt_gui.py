"""PySide6 desktop interface for MySQL Anonymized Copy."""

from __future__ import annotations

import copy
import faulthandler
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal, QDateTime
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog,
    QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QScrollArea, QSplitter, QTabWidget, QTableWidget, QInputDialog, QDateTimeEdit, QMenu,
    QStyledItemDelegate, QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QToolButton,
    QVBoxLayout, QWidget,
)
from PySide6.QtGui import QStandardItem, QStandardItemModel

from .cli import check_config
from .core import (
    list_mapping_fields, load_json, lookup_mapping, prune_rules_to_schema,
    save_json_atomic,
)
from .schema import expand_related_tables


class TaskSignals(QObject):
    progress = Signal(object)
    result = Signal(object)
    error = Signal(str, str, str)
    finished = Signal()


class Task(QRunnable):
    def __init__(self, operation: Callable[..., Any], *args: Any, **kwargs: Any):
        super().__init__()
        self.operation, self.args, self.kwargs = operation, args, kwargs
        self.signals = TaskSignals()

    def run(self) -> None:
        try:
            self.signals.result.emit(self.operation(*self.args, progress=self.signals.progress.emit,
                                                    **self.kwargs))
        except Exception as exc:
            self.signals.error.emit(getattr(self.operation, "__name__", type(self.operation).__name__),
                                    str(exc), traceback.format_exc())
        finally:
            self.signals.finished.emit()


class TableSizeDelegate(QStyledItemDelegate):
    """Render numeric size sort values as readable MiB strings."""

    def displayText(self, value: Any, locale) -> str:
        if isinstance(value, (int, float)):
            return "—" if value < 0 else f"{value / (1024 * 1024):.1f} MiB"
        return str(value)


class BrowseDataTable(QTableWidget):
    """Allow Shift+wheel to scroll wide browse results horizontally."""

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            delta = event.pixelDelta().y() or event.angleDelta().y()
            if delta:
                bar = self.horizontalScrollBar()
                bar.setValue(bar.value() - delta)
                event.accept()
                return
        super().wheelEvent(event)


class CheckableComboBox(QComboBox):
    """Combo box whose entries can be selected independently."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setModel(QStandardItemModel(self))
        self.setEditable(True)
        self.lineEdit().setReadOnly(True)
        self.lineEdit().setPlaceholderText("All columns")
        self.view().clicked.connect(self._normalize_selection)

    def add_checkable_item(self, text: str, data: Any = None, checked: bool = False) -> None:
        item = QStandardItem(text)
        item.setData(data, Qt.ItemDataRole.UserRole)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
        item.setData(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked,
                     Qt.ItemDataRole.CheckStateRole)
        self.model().appendRow(item)
        self._update_text()

    def _normalize_selection(self, index) -> None:
        item = self.model().itemFromIndex(index)
        if item is None:
            return
        if item.data(Qt.ItemDataRole.UserRole) is None and item.checkState() == Qt.CheckState.Checked:
            for row in range(1, self.count()):
                self.model().item(row).setCheckState(Qt.CheckState.Unchecked)
        elif item.data(Qt.ItemDataRole.UserRole) is not None and item.checkState() == Qt.CheckState.Checked:
            self.model().item(0).setCheckState(Qt.CheckState.Unchecked)
        elif not any(self.model().item(row).checkState() == Qt.CheckState.Checked
                     for row in range(1, self.count())):
            self.model().item(0).setCheckState(Qt.CheckState.Checked)
        self._update_text()

    def _update_text(self) -> None:
        selected = [self.model().item(i).text() for i in range(self.count())
                    if self.model().item(i).checkState() == Qt.CheckState.Checked]
        self.lineEdit().setText(", ".join(selected) if selected else "All columns")

    def checked_data(self) -> list[str]:
        return [self.itemData(i) for i in range(self.count())
                if self.model().item(i).checkState() == Qt.CheckState.Checked
                and self.itemData(i) is not None]

    def set_checked_data(self, values: list[str]) -> None:
        wanted = set(values)
        for i in range(self.count()):
            item = self.model().item(i)
            data = self.itemData(i)
            checked = (not wanted) if data is None else data in wanted
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self._update_text()


class AnonymizerWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("MySQL Anonymized Copy")
        self.resize(1180, 820)
        self.setMinimumSize(760, 560)
        self.pool = QThreadPool.globalInstance()
        self._active_task: Task | None = None
        self._copy_progress_timer: QTimer | None = None
        self._copy_process: subprocess.Popen | None = None
        self._copy_worker_paths: dict[str, Path] | None = None
        self._copy_stderr_handle = None
        self._copy_progress_mtime_ns: int | None = None
        self._active_worker_mode = "copy"
        self._last_progress_ui_update = 0.0
        self.busy = False
        self.tables: list[dict[str, Any]] = []
        self.table_names: list[str] = []
        self.destination_tables: set[str] = set()
        self.copied_tables: set[str] = set()
        self.browse_tables: dict[str, list[dict[str, Any]]] = {"source": [], "destination": []}
        self.mapping_fields: dict[str, list[str]] = {}
        self._pending_lookup_selection: tuple[str, str] | None = None
        self._pending_browse_table: str | None = None
        self.copy_resume: dict[str, Any] | None = None
        self.active_copy_table: str | None = None
        self.failed_copy_table: str | None = None
        self.pause_event: threading.Event | None = None
        self.close_when_paused = False
        self.copied_destination_identity: tuple[str, str, str] | None = None
        self.inspection_snapshot: dict[str, Any] | None = None
        self.auto_confirm = False
        self._init_paths()
        self._build_ui()
        self._load_state()
        self.load_config()
        self.load_rules()
        startup_tables_view = copy.deepcopy(getattr(self, "tables_view_state", {}))
        self.load_profiles()
        if getattr(self, "last_profile", "") and self.profile_box.currentText() == self.last_profile:
            self.load_profile()
        if startup_tables_view:
            self._load_tables_view_config(startup_tables_view)
            self.save_state()
        QTimer.singleShot(250, self._offer_resume)

    @staticmethod
    def _state_file() -> Path:
        if os.name == "nt":
            root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        elif sys.platform == "darwin":
            root = Path.home() / "Library" / "Application Support"
        else:
            root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        return root / "mysql-anonymized-copy" / "gui_state.json"

    @classmethod
    def _checkpoint_path(cls) -> Path:
        return cls._state_file().with_name("copy_resume.json")

    @classmethod
    def _error_log_path(cls) -> Path:
        return cls._state_file().with_name("gui_errors.log")

    @classmethod
    def _activity_log_path(cls) -> Path:
        return cls._state_file().with_name("gui_activity.log")

    @classmethod
    def _write_activity_log(cls, message: str) -> None:
        path = cls._activity_log_path()
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try: path.parent.chmod(0o700)
            except OSError: pass
            with path.open("a", encoding="utf-8") as log_file:
                os.chmod(path, 0o600)
                log_file.write(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {message}\n")
        except OSError:
            pass

    @staticmethod
    def _process_rss_mb() -> float | None:
        """Read resident memory on Linux without requiring an extra dependency."""
        try:
            with open("/proc/self/status", encoding="ascii") as status:
                for line in status:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) / 1024
        except (OSError, ValueError, IndexError):
            pass
        return None

    @classmethod
    def _write_error_log(cls, operation: str, message: str, traceback_text: str) -> Path:
        log_path = cls._error_log_path()
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry = f"[{timestamp}] {operation}: {message}\n{traceback_text.rstrip()}\n\n"
        log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try: log_path.parent.chmod(0o700)
        except OSError: pass
        with log_path.open("a", encoding="utf-8") as log_file:
            os.chmod(log_path, 0o600)
            log_file.write(entry)
        return log_path

    def _init_paths(self) -> None:
        self.config_path = "config.json"
        self.rules_path = "rules.json"
        self.mapping_path = ""
        self.profile_config: dict[str, Any] | None = None
        self.saved_selection: list[str] = []
        self.fields: dict[str, dict[str, QLineEdit]] = {}
        self.passwords: dict[str, QLineEdit] = {}

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 10, 12, 8)
        visibility = QHBoxLayout()
        visibility.addWidget(QLabel("Show:"))
        for label, attr in (("Setup", "setup_panel"), ("Actions", "actions_panel"),
                            ("Progress", "progress_panel"), ("Activity log", "log"),
                            ("Raw rules JSON", "rules_text")):
            toggle = QToolButton(); toggle.setText(label); toggle.setCheckable(True); toggle.setChecked(True)
            toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            toggle.toggled.connect(lambda shown, name=attr: getattr(self, name).setVisible(shown))
            visibility.addWidget(toggle)
        visibility.addStretch(1)
        root.addLayout(visibility)
        paths = QFormLayout()
        self.config_edit = self._path_edit(self.config_path)
        config_row = self._browse_row(self.config_edit, self.choose_config, self.save_config_as)
        paths.addRow("Config", config_row)
        self.rules_edit = self._path_edit(self.rules_path)
        rules_row = self._browse_row(self.rules_edit, self.choose_rules, self.save_rules_as)
        paths.addRow("Rules", rules_row)
        self.mapping_edit = QLineEdit()
        paths.addRow("Mapping store (optional)", self._browse_row(self.mapping_edit, self.choose_mapping))
        self.profile_box = QComboBox()
        profile_row = QWidget(); profile_layout = QHBoxLayout(profile_row); profile_layout.setContentsMargins(0, 0, 0, 0)
        profile_layout.addWidget(self.profile_box, 1)
        self.profile_save_btn = QPushButton("Save profile…"); self.profile_save_btn.clicked.connect(self.save_profile)
        self.profile_load_btn = QPushButton("Load profile"); self.profile_load_btn.clicked.connect(self.load_profile)
        self.profile_box.currentTextChanged.connect(self._profile_changed)
        profile_layout.addWidget(self.profile_load_btn); profile_layout.addWidget(self.profile_save_btn)
        paths.addRow("App profile", profile_row)
        self.setup_panel = QWidget(); self.setup_panel.setLayout(paths)
        root.addWidget(self.setup_panel)

        actions = QHBoxLayout()
        self.save_config_btn = QPushButton("Save config"); self.save_config_btn.clicked.connect(self.save_config)
        self.inspect_btn = QPushButton("Inspect source"); self.inspect_btn.clicked.connect(self.inspect)
        self.test_btn = QPushButton("Test connections"); self.test_btn.clicked.connect(self.test_connections)
        self.auto_check = QCheckBox("Auto-confirm copy/table actions"); self.auto_check.toggled.connect(self._auto_changed)
        self.copy_btn = QPushButton("Copy selected tables"); self.copy_btn.clicked.connect(self.copy_selected)
        self.copy_btn.setObjectName("primaryButton")
        self.pause_btn = QPushButton("Pause copy"); self.pause_btn.setEnabled(False); self.pause_btn.clicked.connect(self.pause_or_resume)
        self.skip_failed_btn = QPushButton("Remove failed table and continue")
        self.skip_failed_btn.setEnabled(False); self.skip_failed_btn.clicked.connect(self.discard_failed_table)
        for button in (self.save_config_btn, self.inspect_btn, self.test_btn): actions.addWidget(button)
        actions.addWidget(self.auto_check); actions.addStretch(1); actions.addWidget(self.pause_btn); actions.addWidget(self.copy_btn)
        actions.addWidget(self.skip_failed_btn)
        self.actions_panel = QWidget(); self.actions_panel.setLayout(actions)
        root.addWidget(self.actions_panel)
        self.overall_title = QLabel("Overall progress")
        self.overall_eta = QLabel("ETA: —")
        self.overall_progress = QProgressBar(); self.overall_progress.setRange(0, 1000)
        self.table_title = QLabel("Current table")
        self.table_eta = QLabel("ETA: —")
        self.table_progress = QProgressBar(); self.table_progress.setRange(0, 1000)
        self._overall_started_at: float | None = None
        self._overall_initial_rows = 0
        self._overall_initial_tables = 0
        self._table_started_at: float | None = None
        self.status = QLabel("Load connection settings, then inspect the source.")
        self.progress_panel = QWidget(); progress_layout = QVBoxLayout(self.progress_panel)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        progress_layout.addWidget(self.overall_title); progress_layout.addWidget(self.overall_eta)
        progress_layout.addWidget(self.overall_progress)
        progress_layout.addWidget(self.table_title); progress_layout.addWidget(self.table_eta)
        progress_layout.addWidget(self.table_progress)
        root.addWidget(self.progress_panel); root.addWidget(self.status)

        self.tabs = QTabWidget(); root.addWidget(self.tabs, 1)
        self.connections_tab = QWidget(); self.tables_tab = QWidget(); self.rules_tab = QWidget()
        self.lookup_tab = QWidget(); self.browse_tab = QWidget()
        for page, title in ((self.connections_tab, "Connections"), (self.tables_tab, "Tables"),
                            (self.rules_tab, "Anonymization rules"), (self.lookup_tab, "Lookup"),
                            (self.browse_tab, "Browse data")):
            self.tabs.addTab(page, title)
        self._build_connections(); self._build_tables(); self._build_rules(); self._build_lookup(); self._build_browse()

        self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setMaximumBlockCount(3000)
        self.log.setMaximumHeight(105); root.addWidget(self.log)
        self.setCentralWidget(central)
        self.setStyleSheet("""
            QWidget { font-size: 10pt; }
            QGroupBox { font-weight: 600; border: 1px solid #d5dce6; border-radius: 5px; margin-top: 9px; padding: 8px; }
            QGroupBox::title { subcontrol-origin: margin; left: 9px; padding: 0 4px; }
            QHeaderView::section { background: #e9eef5; padding: 6px; border: 0; font-weight: 600; }
            QPushButton#primaryButton { color: white; background: #2563eb; padding: 7px 14px; font-weight: 600; }
            QPushButton#saveRulesButton[unsaved="true"] { color: white; background: #c2410c; border: 1px solid #9a3412; font-weight: 600; }
        """)

    @staticmethod
    def _path_edit(value: str) -> QLineEdit:
        edit = QLineEdit(value); edit.setClearButtonEnabled(True); return edit

    @staticmethod
    def _browse_row(edit: QLineEdit, browse: Callable, save: Callable | None = None) -> QWidget:
        row = QWidget(); layout = QHBoxLayout(row); layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit, 1)
        button = QPushButton("Browse…"); button.clicked.connect(browse); layout.addWidget(button)
        if save:
            extra = QPushButton("Save as…"); extra.clicked.connect(save); layout.addWidget(extra)
        return row

    def _scroll_page(self, page: QWidget) -> QVBoxLayout:
        outer = QVBoxLayout(page); outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        body = QWidget(); self._page_body_layout = QVBoxLayout(body); self._page_body_layout.setContentsMargins(10, 10, 10, 10)
        scroll.setWidget(body); outer.addWidget(scroll); return self._page_body_layout

    def _build_connections(self) -> None:
        layout = self._scroll_page(self.connections_tab)
        splitter = QSplitter(Qt.Horizontal)
        for role in ("source", "destination"):
            group = QGroupBox(role.title()); form = QFormLayout(group)
            fields: dict[str, QLineEdit] = {}
            for key, default in (("host", "127.0.0.1"), ("port", "3306"), ("database", ""), ("user", "")):
                field = QLineEdit(default); form.addRow(key.title(), field); fields[key] = field
            password = QLineEdit(); password.setEchoMode(QLineEdit.Password); form.addRow("Password", password)
            form.addRow(QLabel("Leave blank to use password_env from config."))
            self.fields[role] = fields; self.passwords[role] = password; splitter.addWidget(group)
        layout.addWidget(splitter); layout.addStretch(1)

    def _build_tables(self) -> None:
        layout = self._scroll_page(self.tables_tab)
        bar = QHBoxLayout()
        self.select_all_btn = QPushButton("Select all"); self.select_all_btn.clicked.connect(lambda: self.table_view.selectAll())
        self.clear_selection_btn = QPushButton("Clear selection"); self.clear_selection_btn.clicked.connect(lambda: self.table_view.clearSelection())
        self.include_check = QCheckBox("Select directly related tables automatically"); self.include_check.setChecked(True)
        self.include_check.setToolTip("For copying, also select tables referenced by the selection and tables that reference it. Recreating selected tables always includes the full connected set of related tables.")
        self.allow_locks_check = QCheckBox("Allow source table locks for non-InnoDB")
        self.selected_top_check = QCheckBox("Show selected tables at top"); self.selected_top_check.toggled.connect(self.render_tables)
        self.hide_copied_check = QCheckBox("Hide copied tables"); self.hide_copied_check.toggled.connect(self.render_tables)
        self.recreate_tables_btn = QPushButton("Recreate selected tables"); self.recreate_tables_btn.clicked.connect(self.recreate_tables)
        self.recreate_db_btn = QPushButton("Recreate destination DB"); self.recreate_db_btn.clicked.connect(self.recreate_database)
        self.clear_data_btn = QPushButton("Clear destination data"); self.clear_data_btn.clicked.connect(self.clear_destination)
        for widget in (self.select_all_btn, self.clear_selection_btn, self.include_check,
                       self.allow_locks_check, self.selected_top_check, self.hide_copied_check): bar.addWidget(widget)
        layout.addLayout(bar)
        range_box = QGroupBox("Copy a date/time range and its related rows")
        range_layout = QGridLayout(range_box)
        self.date_range_check = QCheckBox("Enable relationship-preserving date range")
        self.date_range_check.setEnabled(False)
        self.date_range_check.toggled.connect(self._date_range_toggled)
        self.date_range_start = QDateTimeEdit(QDateTime.currentDateTime().addDays(-7)); self.date_range_start.setCalendarPopup(True)
        self.date_range_end = QDateTimeEdit(QDateTime.currentDateTime()); self.date_range_end.setCalendarPopup(True)
        for edit in (self.date_range_start, self.date_range_end):
            edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
            edit.setToolTip("Set both the date and time. Edit the time with the keyboard or the field controls.")
        range_layout.addWidget(self.date_range_check, 0, 0, 1, 4)
        range_layout.addWidget(QLabel("Start date and time (inclusive)"), 1, 0); range_layout.addWidget(self.date_range_start, 1, 1)
        range_layout.addWidget(QLabel("End date and time (inclusive)"), 1, 2); range_layout.addWidget(self.date_range_end, 1, 3)
        range_layout.addWidget(QLabel("Every checked table with date/time columns uses this interval; checked tables without them are copied in full. The app follows their foreign keys to required parent rows. Check child tables you want included; other child tables are not scanned."), 2, 0, 1, 4)
        layout.addWidget(range_box)
        self._date_range_controls = (self.date_range_start, self.date_range_end)
        self._date_range_toggled(False)
        self.date_range_check.toggled.connect(self._tables_view_changed)
        self.date_range_start.dateTimeChanged.connect(self._tables_view_changed)
        self.date_range_end.dateTimeChanged.connect(self._tables_view_changed)
        self.include_check.toggled.connect(self._tables_view_changed)
        self.selected_top_check.toggled.connect(self._tables_view_changed)
        self.hide_copied_check.toggled.connect(self._tables_view_changed)
        danger = QHBoxLayout(); danger.addStretch(1)
        for widget in (self.clear_data_btn, self.recreate_tables_btn, self.recreate_db_btn): danger.addWidget(widget)
        layout.addLayout(danger)
        filter_row = QHBoxLayout(); filter_row.addWidget(QLabel("Filter tables"))
        self.table_filter = QLineEdit(); self.table_filter.setPlaceholderText("Search table names"); self.table_filter.textChanged.connect(self.render_tables)
        self.table_filter.textChanged.connect(self._tables_view_changed)
        filter_row.addWidget(self.table_filter, 1); layout.addLayout(filter_row)
        self.table_view = QTreeWidget(); self.table_view.setHeaderLabels(["Destination", "Table", "Records", "Size", "References"])
        self.table_view.setSelectionMode(QTreeWidget.ExtendedSelection); self.table_view.setSortingEnabled(True)
        self.table_view.setItemDelegateForColumn(3, TableSizeDelegate(self.table_view))
        self.table_view.header().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table_view.setMinimumHeight(300); layout.addWidget(self.table_view, 1)
        self.table_view.itemSelectionChanged.connect(self._selected_items_changed)
        layout.addWidget(QLabel("✓ marks tables present in the destination. Foreign-key dependencies are validated before copy."))

    def _build_rules(self) -> None:
        layout = self._scroll_page(self.rules_tab)
        actions = QHBoxLayout()
        for title, callback in (("Load rules", self.load_rules), ("Generate rules from schema", self.generate_rules)):
            button = QPushButton(title); button.clicked.connect(callback); actions.addWidget(button)
        self.save_rules_btn = QPushButton("Save rules")
        self.save_rules_btn.setObjectName("saveRulesButton")
        self.save_rules_btn.clicked.connect(self.save_rules)
        actions.addWidget(self.save_rules_btn)
        actions.addStretch(1); layout.addLayout(actions)
        controls = QHBoxLayout()
        self.rule_search = QLineEdit(); self.rule_search.setPlaceholderText("Search tables or columns…"); self.rule_search.textChanged.connect(self.refresh_rules)
        self.rule_status = QComboBox(); self.rule_status.addItems(["All statuses", "Anonymized", "Preserved", "Unconfigured", "Needs attention"]); self.rule_status.currentTextChanged.connect(self.refresh_rules)
        self.rule_compatibility_status_filter = QComboBox()
        for label, value in (("All compatibility", "all"), ("Compatible", "compatible"),
                             ("Incompatible", "incompatible"), ("Not checked", "not_checked")):
            self.rule_compatibility_status_filter.addItem(label, value)
        self.rule_compatibility_status_filter.currentIndexChanged.connect(self.refresh_rules)
        controls.addWidget(self.rule_search, 1); controls.addWidget(self.rule_status)
        controls.addWidget(self.rule_compatibility_status_filter); layout.addLayout(controls)
        self.rule_tree = QTreeWidget(); self.rule_tree.setHeaderLabels(["Table", "Column", "Source type", "Output type", "Compatibility", "Rule status"])
        self.rule_tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.rule_tree.setSortingEnabled(True)
        self.rule_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.rule_tree.customContextMenuRequested.connect(self.show_rule_context_menu)
        for index in (0, 1, 2, 3, 4): self.rule_tree.header().setSectionResizeMode(index, QHeaderView.ResizeToContents)
        self.rule_tree.header().setSectionResizeMode(5, QHeaderView.Stretch)
        self.rule_tree.setMinimumHeight(240); layout.addWidget(self.rule_tree, 1)
        self.rule_mode = QComboBox(); self.rule_mode.addItems(["Unconfigured", "Preserve", "Anonymize"])
        self.rule_type = QComboBox(); self.rule_type.addItems(["email", "url", "phone", "payment_key", "password", "card_mask", "card_number", "card_payexpirydate", "card_validitydate", "card_expirydate", "card_securitycode", "card_type", "description", "name", "entity_name", "address", "file_name", "ip_address", "date_shift", "constant", "unique", "passport", "pnr_code", "location_generalization", "numeric_variance", "coordinate_round", "date_round"])
        self._all_rule_types = [self.rule_type.itemText(i) for i in range(self.rule_type.count())]
        self._rule_type_before_filter = ""
        self.rule_compatibility_filter = QCheckBox("Compatible types only")
        self.rule_compatibility_filter.setToolTip("Limit transformation choices to types compatible with every selected field. For constant, enter a JSON value first so its type can be checked; turn this off to configure an untyped value.")
        self.rule_constant = QLineEdit(); self.rule_constant.setPlaceholderText("JSON value for constant")
        self.rule_type.currentTextChanged.connect(self.update_rule_options_placeholder)
        editrow = QHBoxLayout(); editrow.addWidget(QLabel("Mode")); editrow.addWidget(self.rule_mode); editrow.addWidget(QLabel("Transform")); editrow.addWidget(self.rule_type); editrow.addWidget(self.rule_compatibility_filter); editrow.addWidget(self.rule_constant, 1)
        self.apply_rule_btn = QPushButton("Apply to selected fields"); self.apply_rule_btn.clicked.connect(self.apply_rule); editrow.addWidget(self.apply_rule_btn)
        remove_btn = QPushButton("Remove rule"); remove_btn.clicked.connect(self.remove_rule); editrow.addWidget(remove_btn); layout.addLayout(editrow)
        self.rule_compatibility_notice = QLabel()
        self.rule_compatibility_notice.setObjectName("ruleCompatibilityNotice")
        self.rule_compatibility_notice.setWordWrap(True)
        self.rule_compatibility_notice.setStyleSheet("QLabel#ruleCompatibilityNotice { color: #991b1b; background: #fef2f2; border: 1px solid #fecaca; border-radius: 4px; padding: 6px; }")
        self.rule_compatibility_notice.hide()
        layout.addWidget(self.rule_compatibility_notice)
        self.rules_text = QPlainTextEdit(); self.rules_text.setMinimumHeight(280)
        self._rules_refresh_timer = QTimer(self); self._rules_refresh_timer.setSingleShot(True); self._rules_refresh_timer.setInterval(250); self._rules_refresh_timer.timeout.connect(self.refresh_rules)
        self.rules_text.textChanged.connect(lambda: self._rules_refresh_timer.start())
        self.rules_text.textChanged.connect(self._update_save_rules_button)
        self.rule_type.currentTextChanged.connect(self._update_rule_compatibility_notice)
        self.rule_type.currentTextChanged.connect(self._rule_type_selection_changed)
        self.rule_type.currentTextChanged.connect(self._update_rule_type_filter)
        self.rule_constant.textChanged.connect(self._update_rule_compatibility_notice)
        self.rule_constant.textChanged.connect(self._update_rule_type_filter)
        self.rule_mode.currentTextChanged.connect(self._update_rule_compatibility_notice)
        self.rule_mode.currentTextChanged.connect(self._update_rule_type_filter)
        self.rule_compatibility_filter.toggled.connect(self._update_rule_type_filter)
        layout.addWidget(self.rules_text, 2)
        self.rule_tree.itemSelectionChanged.connect(self.load_rule_selection)
        self._saved_rules_snapshot = self._rules_snapshot(self.rules_text.toPlainText())
        self._update_save_rules_button()

    @staticmethod
    def _rules_snapshot(text: str) -> str:
        """Compare rule content while ignoring JSON formatting differences."""
        try:
            return json.dumps(json.loads(text or "{}"), sort_keys=True, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            return text

    def _update_save_rules_button(self) -> None:
        if not hasattr(self, "save_rules_btn") or not hasattr(self, "rules_text"):
            return
        unsaved = self._rules_snapshot(self.rules_text.toPlainText()) != self._saved_rules_snapshot
        self.save_rules_btn.setProperty("unsaved", unsaved)
        self.save_rules_btn.setText("Save rules • Unsaved" if unsaved else "Save rules")
        self.save_rules_btn.setToolTip("Rules have unsaved changes" if unsaved else "Rules are saved")
        self.save_rules_btn.style().unpolish(self.save_rules_btn)
        self.save_rules_btn.style().polish(self.save_rules_btn)

    def _build_lookup(self) -> None:
        layout = self._scroll_page(self.lookup_tab)
        form = QFormLayout()
        self.lookup_table_filter = QLineEdit(); self.lookup_table_filter.setPlaceholderText("Filter tables")
        self.lookup_table = QComboBox(); self.lookup_table.setEditable(False)
        self.lookup_column_filter = QLineEdit(); self.lookup_column_filter.setPlaceholderText("Filter fields")
        self.lookup_column = QComboBox(); self.lookup_value = QLineEdit()
        self.lookup_value.textChanged.connect(self._accept_browse_cell_payload)
        self.lookup_direction = QComboBox(); self.lookup_direction.addItem("Original → anonymized", "original-to-anonymized"); self.lookup_direction.addItem("Anonymized → original", "anonymized-to-original")
        for label, widget in (("Filter tables", self.lookup_table_filter), ("Table", self.lookup_table), ("Filter fields", self.lookup_column_filter), ("Field", self.lookup_column), ("Exact value", self.lookup_value), ("Direction", self.lookup_direction)):
            form.addRow(label, widget)
        layout.addLayout(form)
        buttons = QHBoxLayout(); load_btn = QPushButton("Load fields"); load_btn.clicked.connect(self.load_lookup_fields)
        find_btn = QPushButton("Find mapping"); find_btn.clicked.connect(self.lookup)
        reset = QPushButton("Reset filters"); reset.clicked.connect(self.reset_lookup)
        buttons.addWidget(load_btn); buttons.addWidget(find_btn); buttons.addWidget(reset); buttons.addStretch(1); layout.addLayout(buttons)
        self.lookup_table_filter.textChanged.connect(self.filter_lookup_tables); self.lookup_column_filter.textChanged.connect(self.filter_lookup_columns)
        self.lookup_table.currentTextChanged.connect(self.filter_lookup_columns)
        layout.addWidget(QLabel("Searches the selected SQLite mapping store. Matching is exact.")); layout.addStretch(1)

    def _build_browse(self) -> None:
        layout = self._scroll_page(self.browse_tab)
        form = QGridLayout()
        self.browse_role = QComboBox(); self.browse_role.addItems(["source", "destination"]); self.browse_role.currentTextChanged.connect(self.filter_browse_tables)
        self.browse_table = QComboBox(); self.browse_table.currentTextChanged.connect(self._browse_table_changed)
        self.browse_filter = QLineEdit(); self.browse_filter.setPlaceholderText("Filter tables"); self.browse_filter.textChanged.connect(self.filter_browse_tables)
        self.browse_search_all = QLineEdit(); self.browse_search_all.setPlaceholderText("Search all columns")
        self.browse_column = CheckableComboBox()
        self.browse_column.add_checkable_item("All columns", None, checked=True)
        self.browse_column_value = QLineEdit(); self.browse_column_value.setPlaceholderText("Column contains…")
        load = QPushButton("Load tables"); load.clicked.connect(self.load_browse_tables)
        search = QPushButton("Search / refresh"); search.clicked.connect(self.search_browse)
        reset_column = QPushButton("Reset column filter"); reset_column.clicked.connect(self.reset_browse_column_filter)
        form.addWidget(QLabel("Database"), 0, 0); form.addWidget(self.browse_role, 0, 1); form.addWidget(load, 0, 2)
        form.addWidget(QLabel("Table"), 0, 3); form.addWidget(self.browse_table, 0, 4, 1, 2)
        form.addWidget(QLabel("Filter tables"), 1, 0); form.addWidget(self.browse_filter, 1, 1, 1, 5)
        form.addWidget(QLabel("Search all columns"), 2, 0); form.addWidget(self.browse_search_all, 2, 1, 1, 5)
        form.addWidget(QLabel("Column filter"), 3, 0); form.addWidget(self.browse_column, 3, 1, 1, 2); form.addWidget(self.browse_column_value, 3, 3); form.addWidget(reset_column, 3, 4); form.addWidget(search, 3, 5)
        layout.addLayout(form)
        self.browse_view = BrowseDataTable(); self.browse_view.setAlternatingRowColors(True); self.browse_view.setMinimumHeight(360)
        self.browse_view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        copy_cell = QPushButton("Copy selected cell for Lookup")
        copy_cell.setToolTip("Copy the selected cell with its table and column so it can be pasted into Lookup.")
        copy_cell.clicked.connect(self.copy_browse_cell_for_lookup)
        layout.addWidget(copy_cell)
        layout.addWidget(self.browse_view, 1)
        self.browse_search_all.returnPressed.connect(self.search_browse); self.browse_column_value.returnPressed.connect(self.search_browse)

    # Connection settings and preferences
    def _load_state(self) -> None:
        try:
            state = json.loads(self._state_file().read_text(encoding="utf-8"))
            if isinstance(state, dict):
                self.config_path = state.get("config_path", self.config_path); self.rules_path = state.get("rules_path", self.rules_path)
                self.mapping_path = state.get("mapping_path", ""); self.auto_confirm = bool(state.get("auto_confirm_operations", False))
                self.saved_selection = state.get("selected_tables", []) if isinstance(state.get("selected_tables"), list) else []
                copied = state.get("copied_tables", [])
                self.copied_tables = set(copied) if isinstance(copied, list) else set()
                identity = state.get("copied_destination_identity")
                self.copied_destination_identity = tuple(identity) if isinstance(identity, list) and len(identity) == 3 else None
                self.inspection_snapshot = state.get("inspection_snapshot")
                self.last_profile = state.get("last_profile", "")
                self.tables_view_state = state.get("tables_view", {}) if isinstance(state.get("tables_view"), dict) else {}
                self.selected_top_check.blockSignals(True)
                self.selected_top_check.setChecked(bool(state.get("show_selected_tables_on_top", False)))
                self.selected_top_check.blockSignals(False)
        except (OSError, json.JSONDecodeError, AttributeError): pass
        self.config_edit.setText(self.config_path); self.rules_edit.setText(self.rules_path); self.mapping_edit.setText(self.mapping_path)
        self.auto_check.blockSignals(True)
        self.auto_check.setChecked(self.auto_confirm)
        self.auto_check.blockSignals(False)

    def save_state(self) -> None:
        path = self._state_file()
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try: path.parent.chmod(0o700)
            except OSError: pass
            state = {"config_path": self.config_edit.text(), "rules_path": self.rules_edit.text(),
                     "mapping_path": self.mapping_edit.text(), "auto_confirm_operations": self.auto_check.isChecked(),
                     "selected_tables": self.selected_table_names() or self.saved_selection,
                     "copied_tables": sorted(self.copied_tables),
                     "copied_destination_identity": list(self.copied_destination_identity) if self.copied_destination_identity else None,
                     "show_selected_tables_on_top": self.selected_top_check.isChecked()}
            state["last_profile"] = self.profile_box.currentText()
            state["tables_view"] = self._tables_view_config()
            if self.inspection_snapshot: state["inspection_snapshot"] = self.inspection_snapshot
            path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8"); path.chmod(0o600)
        except OSError as exc:
            if hasattr(self, "log"): self.log_message(f"Could not save app state: {exc}")

    def _auto_changed(self, value: bool) -> None:
        self.auto_confirm = value; self.save_state()

    def config(self) -> dict[str, Any]:
        try: config = load_json(self.config_edit.text()) if Path(self.config_edit.text()).exists() else {}
        except Exception: config = {}
        if self.profile_config is not None: config = copy.deepcopy(self.profile_config)
        for role in ("source", "destination"):
            settings = dict(config.get(role, {})); settings.pop("password", None)
            for key, field in self.fields[role].items():
                value = field.text().strip()
                settings[key] = int(value) if key == "port" and value else value
            password = self.passwords[role].text()
            if password: settings["password"] = password; settings.pop("password_env", None)
            config[role] = settings
        config["allow_source_table_locks"] = self.allow_locks_check.isChecked()
        config["tables_view"] = {"include_dependencies": self.include_check.isChecked(), "selected_tables": self.selected_table_names() or self.saved_selection}
        check_config(config)
        return config

    def load_config(self) -> None:
        if not Path(self.config_edit.text()).exists(): return
        try:
            config = load_json(self.config_edit.text()); check_config(config)
            self.profile_config = None
            for role in ("source", "destination"):
                settings = config.get(role, {})
                for key, field in self.fields[role].items(): field.setText(str(settings.get(key, "127.0.0.1" if key == "host" else "3306" if key == "port" else "")))
                self.passwords[role].setText(str(settings.get("password", "")))
            self.allow_locks_check.setChecked(bool(config.get("allow_source_table_locks", False)))
            tables_view = config.get("tables_view", {})
            self.include_check.setChecked(bool(tables_view.get("include_dependencies", True)))
            selected = tables_view.get("selected_tables", [])
            self.saved_selection = [name for name in selected if isinstance(name, str)] if isinstance(selected, list) else []
            self.log_message(f"Loaded config from {self.config_edit.text()}.")
        except Exception as exc: self.error("Could not load config", exc)

    def save_config(self) -> None:
        try:
            config = self.config()
            save_json_atomic(self.config_edit.text(), config)
            self.profile_config = copy.deepcopy(config); self.save_state()
            self.log_message(f"Saved connection settings to {self.config_edit.text()}.")
        except Exception as exc: self.error("Could not save config", exc)

    def choose_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open config", self.config_edit.text(), "JSON files (*.json);;All files (*)")
        if path: self.config_edit.setText(path); self.load_config(); self.save_state()

    def save_config_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save config as", self.config_edit.text(), "JSON files (*.json)")
        if path:
            try: self.profile_config = self.config()
            except Exception as exc: return self.error("Could not save config", exc)
            self.config_edit.setText(path); self.save_config()

    def choose_rules(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open rules", self.rules_edit.text(), "JSON files (*.json);;All files (*)")
        if path: self.rules_edit.setText(path); self.load_rules(); self.save_state()

    def save_rules_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save rules as", self.rules_edit.text(), "JSON files (*.json)")
        if path:
            previous_path = self.rules_edit.text()
            self.rules_edit.setText(path)
            if not self.save_rules():
                self.rules_edit.setText(previous_path)

    def choose_mapping(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Select mapping store", self.mapping_edit.text(), "SQLite databases (*.sqlite3 *.sqlite *.db)")
        if not path: return
        try:
            store = open_mapping_store(path); store.close(); self.mapping_edit.setText(path); self.save_state(); self.load_lookup_fields()
        except Exception as exc: self.error("Could not create mapping store", exc)

    def load_rules(self) -> None:
        if not Path(self.rules_edit.text()).exists(): return
        try:
            self.rules_text.blockSignals(True); self.rules_text.setPlainText(json.dumps(load_json(self.rules_edit.text()), indent=2, ensure_ascii=False)); self.rules_text.blockSignals(False); self._saved_rules_snapshot = self._rules_snapshot(self.rules_text.toPlainText()); self._update_save_rules_button(); self.refresh_rules(); self.log_message(f"Loaded rules from {self.rules_edit.text()}.")
        except Exception as exc: self.rules_text.blockSignals(False); self.error("Could not load rules", exc)

    def save_rules(self) -> bool:
        try:
            rules = json.loads(self.rules_text.toPlainText() or "{}")
            if not isinstance(rules, dict): raise ValueError("Rules must be a JSON object")
            save_json_atomic(self.rules_edit.text(), rules)
            self._saved_rules_snapshot = self._rules_snapshot(self.rules_text.toPlainText())
            self._update_save_rules_button()
            self.save_state()
            self._sync_active_profile_rules(rules)
            self.refresh_rules()
            self.log_message(f"Saved rules to {self.rules_edit.text()}.")
            return True
        except Exception as exc:
            self.error("Could not save rules", exc)
            return False

    def _sync_active_profile_rules(self, rules: dict[str, Any]) -> None:
        """Keep a saved profile from restoring an older rules snapshot at startup."""
        name = self.profile_box.currentText()
        if not name:
            return
        path = self._state_file().parent / "profiles" / f"{quote(name, safe='')}.json"
        if not path.exists():
            return
        try:
            profile = load_json(str(path))
            if not isinstance(profile, dict):
                return
            profile["rules"] = copy.deepcopy(rules)
            profile["rules_path"] = self.rules_edit.text()
            save_json_atomic(str(path), profile)
            path.chmod(0o600)
        except Exception as exc:
            self.log_message(f"Saved rules file, but could not update profile {name!r}: {exc}")

    def generate_rules(self) -> None:
        if not self.tables: return self.info("Inspect source first", "Inspect the source database before generating rules.")
        self.rules_text.setPlainText(json.dumps(init_rules(self.tables), indent=2, ensure_ascii=False))

    def load_profiles(self) -> None:
        self.profile_box.blockSignals(True)
        self.profile_box.clear()
        directory = self._state_file().parent / "profiles"
        names = []
        if directory.exists():
            for path in directory.glob("*.json"):
                try:
                    profile = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(profile, dict) and isinstance(profile.get("name"), str): names.append(profile["name"])
                except (OSError, json.JSONDecodeError): pass
        self.profile_box.addItems(sorted(names, key=str.casefold))
        last = getattr(self, "last_profile", "")
        if last in names:
            self.profile_box.setCurrentText(last)
        self.profile_box.blockSignals(False)

    def _profile_changed(self, _name: str) -> None:
        self.save_state()

    def _tables_view_changed(self, *_args: Any) -> None:
        self.save_state()

    def _tables_view_config(self) -> dict[str, Any]:
        return {
            "include_dependencies": self.include_check.isChecked(),
            "selected_tables": self.selected_table_names() or self.saved_selection,
            "filter": self.table_filter.text(),
            "hide_copied": self.hide_copied_check.isChecked(),
            "date_range_enabled": self.date_range_check.isChecked(),
            "date_range_start": self.date_range_start.dateTime().toString("yyyy-MM-dd HH:mm:ss"),
            "date_range_end": self.date_range_end.dateTime().toString("yyyy-MM-dd HH:mm:ss"),
        }

    def _load_tables_view_config(self, view: dict[str, Any]) -> None:
        self.tables_view_state = view
        self.saved_selection = [x for x in view.get("selected_tables", []) if isinstance(x, str)] if isinstance(view.get("selected_tables", []), list) else []
        widgets = (self.include_check, self.table_filter, self.hide_copied_check,
                   self.date_range_start, self.date_range_end, self.date_range_check)
        for widget in widgets: widget.blockSignals(True)
        try:
            self.include_check.setChecked(bool(view.get("include_dependencies", True)))
            self.table_filter.setText(str(view.get("filter", "")))
            self.hide_copied_check.setChecked(bool(view.get("hide_copied", False)))
            for key, widget in (("date_range_start", self.date_range_start), ("date_range_end", self.date_range_end)):
                value = view.get(key)
                if isinstance(value, str):
                    parsed = QDateTime.fromString(value, "yyyy-MM-dd HH:mm:ss")
                    if parsed.isValid(): widget.setDateTime(parsed)
            self.date_range_check.setChecked(bool(view.get("date_range_enabled", False)))
        finally:
            for widget in widgets: widget.blockSignals(False)
        self._date_range_toggled(self.date_range_check.isChecked())

    def save_profile(self) -> None:
        name, ok = QInputDialog.getText(self, "Save app profile", "Profile name:")
        if not ok or not name.strip(): return
        try:
            profile = {"version": 1, "name": name.strip(), "config": self.config(), "tables_view": self._tables_view_config(), "rules": json.loads(self.rules_text.toPlainText() or "{}"), "config_path": self.config_edit.text(), "rules_path": self.rules_edit.text(), "mapping_path": self.mapping_edit.text(), "auto_confirm_operations": self.auto_check.isChecked(), "show_selected_tables_on_top": self.selected_top_check.isChecked()}
            path = self._state_file().parent / "profiles"; path.mkdir(mode=0o700, parents=True, exist_ok=True)
            try: path.chmod(0o700)
            except OSError: pass
            target = path / f"{quote(name.strip(), safe='')}.json"
            if target.exists() and not self.confirm("Replace profile", f"A profile named {name.strip()!r} already exists. Replace it?"): return
            save_json_atomic(str(target), profile); target.chmod(0o600); self.load_profiles(); self.profile_box.setCurrentText(name.strip())
        except Exception as exc: self.error("Could not save profile", exc)

    def load_profile(self) -> None:
        name = self.profile_box.currentText()
        if not name: return self.info("Select profile", "Choose a profile first.")
        try:
            path = self._state_file().parent / "profiles" / f"{quote(name, safe='')}.json"; profile = load_json(str(path))
            check_config(profile["config"]); self.profile_config = copy.deepcopy(profile["config"])
            self.config_edit.setText(profile.get("config_path") or "config.json"); self.rules_edit.setText(profile.get("rules_path") or "rules.json"); self.mapping_edit.setText(profile.get("mapping_path") or "")
            for role in ("source", "destination"):
                for key, field in self.fields[role].items(): field.setText(str(profile["config"][role].get(key, "")))
                self.passwords[role].setText(str(profile["config"][role].get("password", "")))
            self.rules_text.setPlainText(json.dumps(profile["rules"], indent=2, ensure_ascii=False)); self.load_config_preferences(profile["config"])
            self.auto_check.setChecked(bool(profile.get("auto_confirm_operations", False)))
            self.selected_top_check.setChecked(bool(profile.get("show_selected_tables_on_top", False)))
            self._load_tables_view_config(profile.get("tables_view", profile["config"].get("tables_view", {})))
            self.render_tables()
            self.save_state()
        except Exception as exc: self.error("Could not load profile", exc)

    def load_config_preferences(self, config: dict[str, Any]) -> None:
        self.allow_locks_check.setChecked(bool(config.get("allow_source_table_locks", False)))
        self.include_check.setChecked(bool(config.get("tables_view", {}).get("include_dependencies", True)))

    # Generic worker dispatch and feedback
    def run_task(self, title: str, operation: Callable, *args, **kwargs) -> None:
        if self.busy: return
        self.busy = True; self.status.setText(title); self._set_busy(True)
        task = Task(operation, *args, **kwargs)
        task.signals.progress.connect(self.on_progress)
        task.signals.result.connect(self.on_result)
        task.signals.error.connect(self.on_error)
        task.signals.finished.connect(self.on_finished)
        # Keep both the QRunnable and its signal object alive until its queued
        # callbacks have been delivered on the GUI thread.
        self._active_task = task
        self.pool.start(task)

    def _set_busy(self, busy: bool) -> None:
        for button in (self.save_config_btn, self.inspect_btn, self.test_btn, self.copy_btn, self.recreate_tables_btn, self.recreate_db_btn, self.clear_data_btn): button.setEnabled(not busy)
        self.tabs.setEnabled(not busy)
        edits = (self.config_edit, self.rules_edit, self.mapping_edit,
                 *(field for fields in self.fields.values() for field in fields.values()),
                 *self.passwords.values())
        for edit in edits:
            edit.setEnabled(not busy)
        self.pause_btn.setEnabled(self.copy_resume is not None or self.pause_event is not None)
        self.skip_failed_btn.setEnabled(not busy and self.copy_resume is not None and self.failed_copy_table is not None)

    def on_finished(self) -> None:
        self.busy = False; self._set_busy(False)
        self._active_task = None
        if self._copy_progress_timer is not None:
            self._copy_progress_timer.stop()
            self._copy_progress_timer.deleteLater()
            self._copy_progress_timer = None
        if self.close_when_paused:
            QTimer.singleShot(0, self.close)

    def on_error(self, operation: str, message: str, traceback_text: str) -> None:
        if operation in {"copy", "copy worker"}:
            self.failed_copy_table = self.active_copy_table
        self.status.setText("Operation failed."); self.log_message("Error: " + message)
        self._write_activity_log(f"operation_failed operation={operation}")
        try:
            log_path = self._write_error_log(operation, message, traceback_text)
        except OSError as exc:
            self.log_message(f"Could not write traceback log: {exc}")
        else:
            self.log_message(f"Traceback saved to {log_path}")
        if self.copy_resume is not None:
            self.overall_eta.setText("ETA: unavailable; resume available")
            self.table_eta.setText("ETA: unavailable; resume available")
            self.pause_event = None; self.pause_btn.setText("Resume copy"); self.pause_btn.setEnabled(True)
            self.skip_failed_btn.setEnabled(self.failed_copy_table is not None)
        if self.close_when_paused: QTimer.singleShot(0, self.close)
        self.error("Operation failed", message)

    def on_result(self, result: Any) -> None:
        if not isinstance(result, tuple) or not result: return
        kind = result[0]
        if kind == "inspection":
            _, self.tables, destination_tables, config = result
            self.destination_tables = set(destination_tables)
            self.table_names = [t["name"] for t in self.tables]
            temporal = {"date", "datetime", "timestamp"}
            date_tables = [table for table in self.tables if any(
                column.get("DATA_TYPE", "").lower() in temporal for column in table.get("columns", [])
            )]
            self.date_range_check.setEnabled(bool(date_tables))
            if not date_tables and self.date_range_check.isChecked():
                self.date_range_check.setChecked(False)
            self.inspection_snapshot = {"source": self.source_identity(config), "fingerprint": self.schema_fingerprint(self.tables)}
            identity = self.destination_identity(config)
            if self.copied_destination_identity != identity: self.copied_tables.clear()
            self.copied_destination_identity = identity
            self.copied_tables.intersection_update(self.destination_tables)
            self.render_tables(); self.refresh_rules(); self.save_state()
            status = f"Inspection complete. Found {len(self.tables)} source tables."
            if not date_tables:
                status += " No tables have DATE, DATETIME, or TIMESTAMP columns for range selection."
            self.status.setText(status)
        elif kind == "connection_report": self.log_message(result[1]); self.info("Connection test", result[1])
        elif kind == "copy":
            _, counts, names, fields, identity = result; self.copied_tables.update(names); self.destination_tables.update(names); self.render_tables(); self.copy_resume = None; self.pause_event = None; self.pause_btn.setText("Pause copy"); self.log_message("Copy complete.\n" + "\n".join(f"  {k}: {v} rows" for k,v in counts.items())); self.status.setText("Copy complete.")
            self.overall_progress.setValue(1000)
            self.overall_eta.setText("ETA: complete"); self.table_eta.setText("ETA: complete")
            self.copied_destination_identity = identity
            self.save_state()
            if fields is not None: self.mapping_fields = fields; self.filter_lookup_tables()
            if self.close_when_paused: QTimer.singleShot(0, self.close)
        elif kind == "paused":
            self.copy_resume = result[1]; self.pause_event = None; self.pause_btn.setText("Resume copy"); self.pause_btn.setEnabled(True); self.status.setText("Copy paused and checkpoint saved.")
            self.overall_eta.setText("ETA: paused"); self.table_eta.setText("ETA: paused")
            if self.close_when_paused: QTimer.singleShot(0, self.close)
        elif kind == "partial_table_discarded":
            table = result[1]
            state = self.copy_resume
            if state is not None:
                state["skipped_tables"] = list(dict.fromkeys(
                    [*state.get("skipped_tables", []), table]
                ))
                state["completed_counts"] = dict(state.get("completed_counts", {}))
                state["completed_counts"].pop(table, None)
                state.pop("paused_table", None)
                state["is_resume"] = True
                self.write_checkpoint(state)
                self.copy_resume = state
            self.destination_tables.discard(table); self.copied_tables.discard(table)
            self.failed_copy_table = None
            self.render_tables()
            if state is not None and state.get("selected"):
                def continue_saved_copy() -> None:
                    self.pause_event = threading.Event(); self.pause_btn.setText("Pause copy")
                    self._launch_copy(state)
                QTimer.singleShot(0, continue_saved_copy)
                self.log_message(f"Removed incomplete table {table!r}; continuing the saved copy.")
            else:
                self.copy_resume = None; self.pause_event = None
                self._checkpoint_path().unlink(missing_ok=True)
                self.log_message(f"Removed incomplete table {table!r}. No tables remain in the saved copy.")
        elif kind == "lookup_fields":
            self.mapping_fields = result[1]; self.filter_lookup_tables(); self.log_message(f"Loaded {len(self.mapping_fields)} mapped tables.")
            if self._pending_lookup_selection is not None:
                table, column = self._pending_lookup_selection; self._pending_lookup_selection = None
                self._select_lookup_field(table, column)
        elif kind == "lookup":
            _, direction, table, column, value, matches = result
            self.log_message("\n".join(matches) if matches else f"No matching value found for {table}.{column} = {value!r}.")
        elif kind == "browse_tables":
            _, role, tables = result; self.browse_tables[role] = tables; self.filter_browse_tables(); self.status.setText(f"Loaded {len(tables)} {role} tables.")
            if role == "source" and self._pending_browse_table:
                table, self._pending_browse_table = self._pending_browse_table, None
                self.browse_source_table(table)
        elif kind == "browse_rows": self.show_browse_rows(result[1], result[2], result[3]); self.status.setText(f"Showing {len(result[2])} rows from {result[1]}.")
        elif kind == "clear": self.copied_tables.clear(); self.destination_tables.difference_update(result[1]); self.render_tables(); self.save_state(); self.log_message(f"Cleared {len(result[1])} destination tables and {result[2]} mapping entries.")

    def on_progress(self, value: dict[str, Any]) -> None:
        event = value.get("event")
        now = time.monotonic()
        if event == "batch" and now - self._last_progress_ui_update < 1.5:
            return
        self._last_progress_ui_update = now
        if event == "inspect":
            self.status.setText(value.get("status", "Inspecting source…")); return
        if event == "copy_started":
            self._overall_started_at = None
            self._overall_initial_rows = 0
            self._overall_initial_tables = 0
            self.overall_eta.setText("ETA: estimating…")
            self._table_started_at = None
            self.table_eta.setText("ETA: waiting for first table")
            # Keep determinate ranges stable during copying. Repeatedly changing
            # QProgressBar ranges from the polling callback triggered a PySide
            # refcount crash in long copy sessions.
            self.overall_progress.setValue(0); self.table_progress.setValue(0); return
        if event == "copy_finished":
            self.overall_progress.setValue(1000)
            self.table_progress.setValue(1000)
            self.overall_eta.setText("ETA: complete"); self.table_eta.setText("ETA: complete")
            return
        if event == "range_scan":
            self.overall_eta.setText("ETA: calculating linked rows…")
            self.table_eta.setText(f"Relationship scan: {value.get('linked_tables', 0)} tables, "
                                    f"{value.get('linked_rows', 0):,} linked rows")
            self.status.setText(
                f"Finding rows for {value.get('scan_table') or value.get('anchor_table', 'the selected table')} — "
                f"relationship pass {value.get('iteration', 0)}"
            )
            return
        total = value.get("total_tables") or 0; complete = value.get("completed_tables") or 0
        total_rows = value.get("total_rows") or 0
        completed_rows = value.get("completed_rows") or 0
        if event == "start":
            self._overall_started_at = time.monotonic()
            self._overall_initial_rows = completed_rows
            self._overall_initial_tables = complete
        if total_rows:
            overall_fraction = completed_rows / total_rows
        elif total:
            overall_fraction = complete / total
        else:
            overall_fraction = 0
        self.overall_progress.setValue(max(0, min(1000, int(1000 * overall_fraction))))
        if event == "start":
            self.overall_eta.setText("ETA: complete" if overall_fraction >= 1 else "ETA: estimating…")
        elif overall_fraction >= 1:
            self.overall_eta.setText("ETA: complete")
        elif self._overall_started_at is not None:
            elapsed = time.monotonic() - self._overall_started_at
            if total_rows:
                progressed = completed_rows - self._overall_initial_rows
                remaining = max(0, total_rows - completed_rows)
            else:
                progressed = complete - self._overall_initial_tables
                remaining = max(0, total - complete)
            if progressed > 0 and elapsed > 0:
                eta_seconds = elapsed * remaining / progressed
                self.overall_eta.setText(f"ETA: about {self._format_duration(eta_seconds)}")
            else:
                self.overall_eta.setText("ETA: estimating…")
        rows, copied = value.get("table_rows") or 0, value.get("copied_rows") or 0
        if event == "table":
            self._table_started_at = time.monotonic()
            self.table_eta.setText("ETA: estimating…")
        elif event == "batch" and self._table_started_at is not None:
            elapsed = time.monotonic() - self._table_started_at
            remaining = max(0, rows - copied)
            if copied > 0 and elapsed > 0:
                eta_seconds = elapsed * remaining / copied
                self.table_eta.setText(f"ETA: about {self._format_duration(eta_seconds)}")
        elif event == "table_done":
            self.table_eta.setText("ETA: complete")
        if rows:
            self.table_progress.setValue(max(0, min(1000, int(1000 * copied / rows))))
        self.status.setText(f"{value.get('table', 'Copying')} — {copied:,}/{rows:,} rows" if rows else str(value.get("event", "Working…")))

    @staticmethod
    def _format_duration(seconds: float) -> str:
        seconds = max(0, int(seconds + 0.5))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}h {minutes}m"
        if minutes:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"

    def log_message(self, message: str) -> None:
        self.log.appendPlainText(str(message))

    def error(self, title: str, message: Any) -> None:
        QMessageBox.critical(self, title, str(message))

    def info(self, title: str, message: str) -> None:
        QMessageBox.information(self, title, str(message))

    def confirm(self, title: str, message: str, default_no: bool = True) -> bool:
        box = QMessageBox(QMessageBox.Question, title, message, QMessageBox.Yes | QMessageBox.No, self)
        box.setDefaultButton(QMessageBox.No if default_no else QMessageBox.Yes)
        return box.exec() == QMessageBox.Yes

    # Schema and table selection
    def inspect(self) -> None:
        try: config = self.config()
        except Exception as exc: return self.error("Invalid connection settings", exc)
        self._launch_database_operation("inspect", {"config": config}, "Inspecting source schema…")

    def test_connections(self) -> None:
        try: config = self.config()
        except Exception as exc: return self.error("Invalid connection settings", exc)
        self._launch_database_operation("test_connections", {"config": config}, "Testing source and destination connections…")

    @staticmethod
    def source_identity(config: dict[str, Any]) -> dict[str, Any]:
        return {k: config["source"].get(k) for k in ("host", "port", "database", "user")}

    @staticmethod
    def destination_identity(config: dict[str, Any]) -> tuple[str, str, str]:
        dest = config["destination"]
        return (str(dest.get("host", "localhost")).casefold(), str(dest.get("port", 3306)), str(dest.get("database", "")).casefold())

    @staticmethod
    def schema_fingerprint(tables: list[dict[str, Any]]) -> str:
        import hashlib
        structure = [{"name": t["name"], "engine": t.get("engine"), "references": sorted(t.get("references", [])), "external_references": sorted(t.get("external_references", [])), "foreign_keys": t.get("foreign_keys", []), "columns": [{k: c.get(k) for k in ("COLUMN_NAME", "DATA_TYPE", "COLUMN_TYPE", "IS_NULLABLE", "COLUMN_KEY", "EXTRA")} for c in t.get("columns", [])]} for t in tables]
        return hashlib.sha256(json.dumps(structure, sort_keys=True, default=str).encode()).hexdigest()

    def _date_range_toggled(self, enabled: bool) -> None:
        for widget in getattr(self, "_date_range_controls", ()):
            widget.setEnabled(enabled)

    def selected_table_names(self) -> list[str]:
        return [item.text(1) for item in self.table_view.selectedItems()]

    def related_table_names(self, names: list[str]) -> list[str]:
        """Return the full same-schema foreign-key-connected set for names."""
        return expand_related_tables(self.tables, names)

    def render_tables(self) -> None:
        if not hasattr(self, "table_view"): return
        selected = set(self.selected_table_names()) | set(self.saved_selection)
        query = self.table_filter.text().casefold() if hasattr(self, "table_filter") else ""
        tables = list(self.tables)
        if self.selected_top_check.isChecked(): tables.sort(key=lambda t: (t["name"] not in selected, t["name"].casefold()))
        items = []
        for table in tables:
            name = table["name"]
            if self.hide_copied_check.isChecked() and name in self.copied_tables: continue
            if query and query not in name.casefold(): continue
            refs = ", ".join(table.get("references", []))
            size = table.get("size_bytes")
            count = table.get("row_count"); count_text = f"~{count:,}" if count is not None and table.get("row_count_is_estimate") else f"{count:,}" if count is not None else "—"
            mark = "✓" if name in self.destination_tables else ""
            item = QTreeWidgetItem([mark, name, count_text, "", refs])
            item.setData(1, Qt.UserRole, name)
            item.setData(3, Qt.DisplayRole, size if isinstance(size, (int, float)) else -1)
            items.append((item, name in selected))
        sorting_enabled = self.table_view.isSortingEnabled()
        self.table_view.blockSignals(True)
        self.table_view.setUpdatesEnabled(False)
        self.table_view.setSortingEnabled(False)
        try:
            self.table_view.clear()
            self.table_view.addTopLevelItems([item for item, _ in items])
            for item, is_selected in items:
                if is_selected: item.setSelected(True)
        finally:
            self.table_view.setSortingEnabled(sorting_enabled)
            self.table_view.blockSignals(False)
            self.table_view.setUpdatesEnabled(True)

    def _selected_items_changed(self) -> None:
        selected = set(self.selected_table_names())
        if selected and self.include_check.isChecked():
            related = set()
            for table in self.tables:
                if table["name"] in selected:
                    related.update(table.get("references", []))
                if selected.intersection(table.get("references", [])):
                    related.add(table["name"])
            selected.update(related)
            self.table_view.blockSignals(True)
            try:
                for index in range(self.table_view.topLevelItemCount()):
                    item = self.table_view.topLevelItem(index)
                    name = item.text(1)
                    if name in selected:
                        item.setSelected(True)
            finally:
                self.table_view.blockSignals(False)
        self.saved_selection = [table["name"] for table in self.tables if table["name"] in selected]
        self.save_state()

    def copy_selected(self) -> None:
        self.copy("copy")

    def copy(self, action: str = "copy") -> None:
        if not self.tables: return self.info("Inspect source first", "Inspect the source database before copying.")
        if self.inspection_snapshot is None: return self.info("Inspect source first", "Inspect the source before copying so the app can verify its schema.")
        if self.copy_resume: return self.info("Resume existing copy", "Resume or finish the saved copy before starting another one.")
        try:
            config = self.config(); raw = self.rules_text.toPlainText().strip(); rules = json.loads(raw) if raw else load_json(self.rules_edit.text())
            if not rules.get("confirm_copy_password_hashes"): raise ValueError("Set confirm_copy_password_hashes=true after reviewing credential retention.")
            if not self.auto_check.isChecked() and action == "copy" and not self.confirm("Confirm copy", "Copy selected data into the destination? Password hashes may be retained according to the rules."): return
            mapping = self.mapping_edit.text().strip() or None
            if mapping is None and not self.auto_check.isChecked() and not self.confirm("No mapping store selected", "The copy will not save lookup mappings. Continue?"): return
            date_range = None
            if self.date_range_check.isChecked() and action in {"copy", "replace_tables"}:
                if self.date_range_start.dateTime() > self.date_range_end.dateTime():
                    raise ValueError("The date range start must be no later than its end")
                temporal = {"date", "datetime", "timestamp"}
                # Recreate selected tables uses the same date-range anchors as a
                # normal copy. Related tables are expanded below, then the copy
                # worker resolves their rows from these anchors' date window.
                checked_tables = self.selected_table_names()
                table_by_name = {table["name"]: table for table in self.tables}
                date_tables = [
                    name for name in checked_tables
                    if any(column.get("COLUMN_KEY") == "PRI"
                           for column in table_by_name.get(name, {}).get("columns", []))
                    and any(column.get("DATA_TYPE", "").lower() in temporal
                            for column in table_by_name.get(name, {}).get("columns", []))
                ]
                full_tables = [name for name in checked_tables if name not in date_tables]
                date_range = {"date_tables": date_tables,
                              "start": self.date_range_start.dateTime().toString("yyyy-MM-dd HH:mm:ss"),
                              "end": self.date_range_end.dateTime().toString("yyyy-MM-dd HH:mm:ss"),
                              "full_tables": full_tables}
            selected = self.table_names if action == "recreate_database" else self.selected_table_names()
            if date_range:
                selected = list(dict.fromkeys([*date_range["date_tables"], *date_range["full_tables"]]))
            else:
                selected = [n for n in selected if n not in self.copied_tables] if action == "copy" else selected
            if action == "replace_tables":
                selected = self.related_table_names(selected)
            if not selected: return self.info("Select tables", "Select tables to copy.")
            collisions = sorted(set(selected) & self.destination_tables) if action == "copy" else []
            if collisions:
                if not date_range and not self.auto_check.isChecked() and not self.confirm("Tables already exist", "These tables already exist in the destination:\n\n" + "\n".join(collisions) + "\n\nMark them as already copied and skip them?"):
                    return self.info("Recreate existing tables", "Use Recreate selected tables to replace tables already in the destination.")
                if not date_range:
                    self.copied_tables.update(collisions)
                    selected = [name for name in selected if name not in collisions]
                    self.copied_destination_identity = self.destination_identity(config)
                    self.render_tables()
                    self.save_state()
                    if not selected: return self.info("Nothing to copy", "All selected tables already exist and were marked as copied.")
            relevant = set(self.table_names) if date_range else set(selected)
            if self.include_check.isChecked() or action == "recreate_database":
                by_name = {t["name"]: t for t in self.tables}; pending = list(relevant)
                while pending:
                    current = pending.pop()
                    for dependency in by_name[current].get("references", []):
                        if dependency not in relevant: relevant.add(dependency); pending.append(dependency)
            rules, stale = prune_rules_to_schema(rules, self.tables, relevant)
            if stale and not self.auto_check.isChecked() and not self.confirm("Rules don’t match this source", "These rule columns are missing and will be ignored:\n\n" + "\n".join(stale[:40]) + "\n\nContinue?"): return
            state = {"version":1,"config":config,"rules":rules,"selected":selected,"mapping_path":mapping,"rules_path":None if stale else self.rules_edit.text(),"allow_locks":self.allow_locks_check.isChecked(),"include_dependencies":self.include_check.isChecked() or action == "recreate_database","completed_counts":{},"recreate_action":action,"date_range":date_range}
            self.write_checkpoint(state); self.copy_resume = state; self.pause_event = threading.Event(); self.pause_btn.setText("Pause copy"); self._launch_copy(state)
        except Exception as exc: self.error("Could not start copy", exc)

    def write_checkpoint(self, state: dict[str, Any]) -> None:
        p = self._checkpoint_path(); p.parent.mkdir(mode=0o700, parents=True, exist_ok=True); save_json_atomic(str(p), state); p.chmod(0o600)

    def _launch_copy(self, state: dict[str, Any]) -> None:
        checkpoint = self._checkpoint_path()
        paths = {
            "checkpoint_path": checkpoint,
            "progress_path": checkpoint.with_name("copy_progress.json"),
            "result_path": checkpoint.with_name("copy_result.json"),
            "pause_path": checkpoint.with_name("copy_pause.request"),
            "job_path": checkpoint.with_name("copy_worker_job.json"),
            "activity_path": self._activity_log_path(),
            "error_path": self._error_log_path(),
        }
        for key in ("progress_path", "result_path", "pause_path", "job_path"):
            paths[key].unlink(missing_ok=True)
        job = {
            key: str(paths[key])
            for key in (
                "checkpoint_path",
                "progress_path",
                "result_path",
                "pause_path",
                "activity_path",
                "error_path",
            )
        }
        job["inspection_snapshot"] = self.inspection_snapshot
        job["parent_pid"] = os.getpid()
        save_json_atomic(str(paths["job_path"]), job)
        paths["job_path"].chmod(0o600)
        self._copy_worker_paths = paths
        self._active_worker_mode = "copy"
        self._copy_progress_mtime_ns = None
        self._copy_progress_timer = QTimer(self)
        self._copy_progress_timer.setInterval(1000)
        self._copy_progress_timer.timeout.connect(self._poll_copy_progress)
        self._copy_progress_timer.start()
        self.busy = True
        self._set_busy(True)
        self.status.setText(
            "Finding rows in the date range and following table relationships…"
            if state.get("date_range") else "Copying selected tables…"
        )
        self.overall_eta.setText("ETA: estimating…")
        self.table_eta.setText("ETA: waiting for first table")
        self.overall_progress.setValue(0)
        self.table_progress.setValue(0)
        self.pause_btn.setText("Pause copy")
        self.pause_btn.setEnabled(True)

        try:
            log_path = paths["error_path"]
            log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                log_path.parent.chmod(0o700)
            except OSError:
                pass
            self._copy_stderr_handle = log_path.open("a", encoding="utf-8", buffering=1)
            os.chmod(log_path, 0o600)
            self._copy_process = subprocess.Popen(
                [sys.executable, "-m", "mysql_anonymizer.copy_worker", str(paths["job_path"])],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=self._copy_stderr_handle,
            )
            self._write_activity_log(f"copy_worker pid={self._copy_process.pid}")
        except Exception as exc:
            self._copy_process = None
            if self._copy_stderr_handle is not None:
                self._copy_stderr_handle.close()
                self._copy_stderr_handle = None
            self.on_error("copy", str(exc), traceback.format_exc())
            self.on_finished()

    def _launch_database_operation(self, operation: str, args: dict[str, Any], title: str) -> None:
        """Run connector-backed work in a process with no Qt objects loaded."""
        checkpoint = self._checkpoint_path()
        paths = {
            "checkpoint_path": checkpoint,
            "progress_path": checkpoint.with_name("copy_progress.json"),
            "result_path": checkpoint.with_name("copy_result.json"),
            "pause_path": checkpoint.with_name("copy_pause.request"),
            "job_path": checkpoint.with_name("copy_worker_job.json"),
            "activity_path": self._activity_log_path(),
            "error_path": self._error_log_path(),
        }
        for key in ("progress_path", "result_path", "pause_path", "job_path"):
            paths[key].unlink(missing_ok=True)
        job = {key: str(paths[key]) for key in (
            "result_path", "activity_path", "error_path",
        )}
        job.update({
            "mode": "operation", "operation": operation, "args": args,
            "parent_pid": os.getpid(),
        })
        save_json_atomic(str(paths["job_path"]), job)
        paths["job_path"].chmod(0o600)
        self._copy_worker_paths = paths
        self._active_worker_mode = "operation"
        self._copy_progress_mtime_ns = None
        self._copy_progress_timer = QTimer(self)
        self._copy_progress_timer.setInterval(1000)
        self._copy_progress_timer.timeout.connect(self._poll_copy_progress)
        self._copy_progress_timer.start()
        self.busy = True
        self._set_busy(True)
        self.status.setText(title)
        try:
            log_path = paths["error_path"]
            log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                log_path.parent.chmod(0o700)
            except OSError:
                pass
            self._copy_stderr_handle = log_path.open("a", encoding="utf-8", buffering=1)
            os.chmod(log_path, 0o600)
            self._copy_process = subprocess.Popen(
                [sys.executable, "-m", "mysql_anonymizer.copy_worker", str(paths["job_path"])],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=self._copy_stderr_handle,
            )
            self._write_activity_log(f"database_worker operation={operation} pid={self._copy_process.pid}")
        except Exception as exc:
            self._copy_process = None
            if self._copy_stderr_handle is not None:
                self._copy_stderr_handle.close()
                self._copy_stderr_handle = None
            self.on_error(operation, str(exc), traceback.format_exc())
            self.on_finished()

    def _poll_copy_progress(self) -> None:
        paths = self._copy_worker_paths
        if paths is None:
            return
        progress_path = paths["progress_path"]
        try:
            modified = progress_path.stat().st_mtime_ns
            if modified != self._copy_progress_mtime_ns:
                progress = json.loads(progress_path.read_text(encoding="utf-8"))
                self._copy_progress_mtime_ns = modified
                if progress.get("event") == "table":
                    self.active_copy_table = progress.get("table")
                self.on_progress(progress)
        except (OSError, json.JSONDecodeError):
            pass

        process = self._copy_process
        if process is None or process.poll() is None:
            return

        self._copy_process = None
        if self._copy_progress_timer is not None:
            self._copy_progress_timer.stop()
            self._copy_progress_timer.deleteLater()
            self._copy_progress_timer = None
        if self._copy_stderr_handle is not None:
            self._copy_stderr_handle.close()
            self._copy_stderr_handle = None
        try:
            result = json.loads(paths["result_path"].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            result = None

        if result and result.get("kind") == "operation":
            self.on_result(tuple(result["result"]))
        elif result and result.get("kind") == "copy":
            counts = result["counts"]
            self.on_result(("copy", counts, set(counts), result.get("fields"), tuple(result["identity"])))
        elif result and result.get("kind") == "paused":
            self.on_result(("paused", result["state"]))
        else:
            if self._active_worker_mode == "copy":
                try:
                    self.copy_resume = load_json(str(paths["checkpoint_path"]))
                except (OSError, json.JSONDecodeError):
                    pass
            if result and result.get("kind") == "error":
                self.on_error(
                    result.get("operation", "copy"),
                    result.get("message", "Operation failed."),
                    result.get("traceback", ""),
                )
            else:
                message = (
                    f"Copy worker exited unexpectedly (exit code {process.returncode}). "
                    f"The saved checkpoint is available to resume. Diagnostics: {paths['error_path']}"
                )
                self.on_error("copy worker", message, message)
        self.on_finished()

    def pause_or_resume(self) -> None:
        if self.copy_resume and self.pause_event is None:
            state = self.copy_resume; self.pause_event = threading.Event(); self.pause_btn.setText("Pause copy"); self._launch_copy(state)
        elif self.pause_event:
            self.pause_event.set()
            if self._copy_worker_paths is not None:
                self._copy_worker_paths["pause_path"].touch(mode=0o600, exist_ok=True)
            self.pause_btn.setEnabled(False); self.status.setText("Pausing after the current batch…")

    def discard_failed_table(self) -> None:
        table = self.failed_copy_table
        state = self.copy_resume
        if not table or state is None:
            return
        if table in state.get("completed_counts", {}):
            return self.error("Cannot remove completed table", "The active table is marked complete in the saved checkpoint.")
        if not self.confirm("Remove failed table", f"Drop the incomplete destination table {table!r}, discard its saved lookup mappings, skip it, and resume the remaining tables?"):
            return
        try:
            config = state["config"]
            self._launch_database_operation(
                "discard_partial_table",
                {"source": config["source"], "destination": config["destination"],
                 "table": table, "mapping": state.get("mapping_path")},
                f"Removing incomplete table {table}…",
            )
        except Exception as exc:
            self.error("Could not remove incomplete table", exc)

    def _offer_resume(self) -> None:
        try:
            state = json.loads(self._checkpoint_path().read_text(encoding="utf-8"))
            if state.get("version") == 1:
                self.copy_resume = state; self.pause_btn.setText("Resume copy"); self.pause_btn.setEnabled(True); self.copy_btn.setEnabled(False)
                if self.confirm("Resume paused copy?", "A copy checkpoint was found. Resume using its saved connection and rule settings? A partial table will be cleared and recopied. The owner-only checkpoint contains the saved connection settings, including any password."):
                    for role in ("source", "destination"):
                        for key, field in self.fields[role].items(): field.setText(str(state["config"][role].get(key, "")))
                        self.passwords[role].setText(str(state["config"][role].get("password", "")))
                    self.mapping_edit.setText(state.get("mapping_path") or "")
                    self.rules_text.setPlainText(json.dumps(state.get("rules", {}), indent=2, ensure_ascii=False))
                    self.pause_event = threading.Event(); self._launch_copy(state)
        except (OSError, json.JSONDecodeError, AttributeError): pass

    def recreate_tables(self) -> None:
        names = self.selected_table_names()
        if not names: return self.info("Select tables", "Select source tables to recreate.")
        names = self.related_table_names(names)
        if self.auto_check.isChecked() or self.confirm("Recreate destination tables", "The selected destination tables will be dropped and recopied:\n\n" + "\n".join(names)):
            self.copy("replace_tables")

    def recreate_database(self) -> None:
        if not self.tables: return self.info("Inspect source first", "Inspect the source before recreating the database.")
        destination = self.fields["destination"]["database"].text().strip()
        if not destination: return self.error("Destination database required", "Enter the destination database name.")
        typed, ok = QInputDialog.getText(self, "Recreate destination database", f"This permanently deletes the destination database and all its contents. Type {destination!r} to continue:")
        if ok and typed == destination: self.copy("recreate_database")

    def clear_destination(self) -> None:
        destination = self.fields["destination"]["database"].text().strip()
        if not destination: return self.error("Destination database required", "Enter the destination database name.")
        typed, ok = QInputDialog.getText(self, "Clear destination data", f"All destination tables and their data will be permanently deleted. Type {destination!r} to continue:")
        if not ok or typed != destination: return
        try: config = self.config()
        except Exception as exc: return self.error("Invalid connection settings", exc)
        mapping = self.mapping_edit.text().strip()
        clear_maps = bool(mapping and self.confirm("Clear mapping store too?", f"This clears every mapping in {mapping}, including other profiles using this file."))
        try: self._checkpoint_path().unlink(missing_ok=True)
        except OSError as exc: return self.error("Could not discard saved copy", exc)
        self.copy_resume = None; self.pause_event = None
        self._launch_database_operation(
            "clear_destination",
            {"destination": config["destination"], "source": config["source"],
             "mapping": mapping if clear_maps else None},
            "Clearing destination data…",
        )

    # Rule navigator/editor
    _rule_text_types = {"char", "varchar", "tinytext", "text", "mediumtext", "longtext"}
    _rule_numeric_types = {"tinyint", "smallint", "mediumint", "int", "integer", "bigint", "decimal", "numeric", "float", "double", "real"}
    _rule_date_types = {"date", "datetime", "timestamp"}
    _rule_text_transforms = {"email", "url", "phone", "payment_key", "password", "card_mask", "card_number", "card_payexpirydate", "card_validitydate", "card_expirydate", "card_securitycode", "card_type", "description", "name", "entity_name", "address", "file_name", "ip_address", "unique", "passport", "pnr_code", "location_generalization"}

    @staticmethod
    def _type_category(data_type: str) -> str:
        kind = data_type.lower()
        if kind in AnonymizerWindow._rule_text_types:
            return "Text"
        if kind in {"enum", "set"}:
            return "Enum/set"
        if kind in AnonymizerWindow._rule_numeric_types:
            return "Numeric"
        if kind in AnonymizerWindow._rule_date_types:
            return "Date/time"
        if kind in {"json"}:
            return "JSON"
        if kind in {"binary", "varbinary", "tinyblob", "blob", "mediumblob", "longblob"}:
            return "Binary"
        if kind in {"bool", "boolean"}:
            return "Boolean"
        return "Other"

    @staticmethod
    def _rule_output_type(rule: dict[str, Any] | None, source_data_type: str = "") -> str:
        if not isinstance(rule, dict):
            return "Unchanged"
        kind = rule.get("type")
        if kind in AnonymizerWindow._rule_text_transforms:
            return "Text"
        if kind in {"numeric_variance", "coordinate_round"}:
            return "Numeric"
        if kind in {"date_shift", "date_round"}:
            return "Date/time"
        if kind == "constant":
            value = rule.get("value")
            if isinstance(value, bool): return "Boolean"
            if isinstance(value, (int, float)): return "Numeric"
            if isinstance(value, str):
                if source_data_type.lower() in AnonymizerWindow._rule_date_types:
                    try:
                        datetime.fromisoformat(value.replace("Z", "+00:00"))
                        return "Date/time"
                    except ValueError:
                        pass
                if source_data_type.lower() == "json": return "JSON"
                return "Text/string"
            if value is None: return "NULL"
            return "JSON"
        return "Unknown"

    @classmethod
    def _rule_compatibility(cls, data_type: str, column: str,
                            rule: dict[str, Any] | None) -> tuple[bool, str]:
        if not isinstance(rule, dict):
            return True, ""
        kind = rule.get("type")
        category = cls._type_category(data_type)
        if kind in cls._rule_text_transforms:
            compatible = category == "Text"
        elif kind in {"numeric_variance", "coordinate_round"}:
            compatible = category == "Numeric"
            if kind == "coordinate_round":
                compatible = compatible and column.lower() in {"lat", "latitude", "lon", "lng", "long", "longitude"}
        elif kind in {"date_shift", "date_round"}:
            compatible = category == "Date/time"
        elif kind == "constant":
            if "value" not in rule:
                return False, "Enter a JSON value to check constant compatibility"
            value = rule.get("value")
            value_category = ("Boolean" if isinstance(value, bool) else
                              "Numeric" if isinstance(value, (int, float)) else
                              "Text" if isinstance(value, str) else
                              "JSON" if isinstance(value, (dict, list)) else "NULL")
            if isinstance(value, str) and category == "Date/time":
                try:
                    datetime.fromisoformat(value.replace("Z", "+00:00"))
                    value_category = "Date/time"
                except ValueError:
                    pass
            compatible = (value is None or value_category == category
                          or (category == "Date/time" and value_category == "Date/time")
                          or (category in {"Text", "Enum/set"} and value_category == "Text")
                          or (category == "JSON" and value_category in {"Boolean", "Numeric", "Text", "JSON", "NULL"}))
        else:
            compatible = False
        if compatible:
            return True, ""
        return False, f"{cls._rule_output_type(rule, data_type)} output is not compatible with {data_type or 'unknown source type'}"

    def _source_column(self, table: str, column: str) -> dict[str, Any] | None:
        for source_table in self.tables:
            if source_table.get("name") == table:
                return next((item for item in source_table.get("columns", [])
                             if item.get("COLUMN_NAME") == column), None)
        return None

    def _update_rule_compatibility_notice(self, *_args) -> None:
        if not hasattr(self, "rule_compatibility_notice"):
            return
        selected = self.rule_tree.selectedItems()
        if not selected or self.rule_mode.currentText() != "Anonymize":
            self.rule_compatibility_notice.hide()
            return
        kind = self.rule_type.currentText()
        if not kind:
            self.rule_compatibility_notice.setText(
                "The saved transformation is incompatible with the selected field. Choose a compatible type or turn off the filter."
            )
            self.rule_compatibility_notice.show()
            return
        rule: dict[str, Any] = {"type": kind}
        if kind == "constant":
            raw = self.rule_constant.text()
            try:
                rule["value"] = json.loads(raw)
            except json.JSONDecodeError:
                rule["value"] = raw
        elif self.rule_constant.text().strip():
            try:
                options = json.loads(self.rule_constant.text())
                if isinstance(options, dict):
                    rule.update(options)
            except json.JSONDecodeError:
                pass
        issues = []
        for item in selected:
            source = self._source_column(item.text(0), item.text(1))
            if source is None:
                continue
            compatible, reason = self._rule_compatibility(source.get("DATA_TYPE", ""), item.text(1), rule)
            if not compatible:
                issues.append(f"{item.text(0)}.{item.text(1)}: {reason}")
        if issues:
            self.rule_compatibility_notice.setText("Incompatible anonymization type: " + "; ".join(issues))
            self.rule_compatibility_notice.show()
        else:
            self.rule_compatibility_notice.hide()

    def _rule_type_selection_changed(self, *_args) -> None:
        if self.rule_type.currentText():
            self._rule_type_before_filter = ""

    def _update_rule_type_filter(self, *_args, preferred: str | None = None) -> None:
        if not hasattr(self, "rule_type"):
            return
        current = preferred or self.rule_type.currentText() or self._rule_type_before_filter
        filtered = self.rule_compatibility_filter.isChecked()
        selected = self.rule_tree.selectedItems()
        options = list(self._all_rule_types)
        if filtered and selected:
            options = []
            for kind in self._all_rule_types:
                candidate: dict[str, Any] = {"type": kind}
                if kind == "constant":
                    raw = self.rule_constant.text()
                    if raw.strip():
                        try:
                            candidate["value"] = json.loads(raw)
                        except json.JSONDecodeError:
                            candidate["value"] = raw
                compatible = True
                for item in selected:
                    source = self._source_column(item.text(0), item.text(1))
                    if source is not None and not self._rule_compatibility(
                            source.get("DATA_TYPE", ""), item.text(1), candidate)[0]:
                        compatible = False
                        break
                if compatible:
                    options.append(kind)
        if current and current not in options and filtered:
            self._rule_type_before_filter = current
        elif not filtered:
            self._rule_type_before_filter = ""
        blocked = self.rule_type.blockSignals(True)
        try:
            self.rule_type.clear()
            self.rule_type.addItems(options)
            if current in options:
                self.rule_type.setCurrentText(current)
            elif current and filtered:
                self.rule_type.setCurrentIndex(-1)
        finally:
            self.rule_type.blockSignals(blocked)
        self.update_rule_options_placeholder(self.rule_type.currentText())
        self.apply_rule_btn.setEnabled(
            not (filtered and self.rule_mode.currentText() == "Anonymize"
                 and selected and not self.rule_type.currentText())
        )
        self._update_rule_compatibility_notice()

    def refresh_rules(self) -> None:
        if not hasattr(self, "rule_tree"): return
        try:
            data = json.loads(self.rules_text.toPlainText() or "{}"); table_rules = data.get("tables", {})
            source = {t["name"]: {c["COLUMN_NAME"]: c for c in t["columns"]} for t in self.tables}
            query = self.rule_search.text().casefold(); status_filter = self.rule_status.currentText()
            compatibility_filter = self.rule_compatibility_status_filter.currentData()
            self.rule_tree.clear()
            for table in sorted(set(source) | set(table_rules)):
                rule = table_rules.get(table, {}); preserve = set(rule.get("preserve", [])); anonymize = rule.get("anonymize", {})
                cols = sorted(set(source.get(table, {})) | preserve | set(anonymize))
                for col in cols:
                    source_column = source.get(table, {}).get(col)
                    if self.tables and table not in source: status = "Table not in source"
                    elif self.tables and source_column is None: status = "Column not in source"
                    elif col in preserve and col in anonymize: status = "Conflict: preserve + anonymize"
                    elif col in anonymize: status = "Anonymized (" + str(anonymize[col].get("type", "unknown")) + ")"
                    elif col in preserve: status = "Preserved"
                    else: status = "Unconfigured"
                    configured_rule = anonymize.get(col) if isinstance(anonymize, dict) else None
                    data_type = source_column.get("DATA_TYPE", "") if source_column else ""
                    source_type = source_column.get("COLUMN_TYPE", data_type) if source_column else "—"
                    output_type = self._rule_output_type(configured_rule, data_type)
                    compatible, reason = self._rule_compatibility(data_type, col, configured_rule)
                    compatibility = "—" if not configured_rule or source_column is None else ("Compatible" if compatible else "Incompatible")
                    if query and query not in f"{table} {col} {source_type} {output_type} {compatibility} {status}".casefold(): continue
                    if compatibility_filter == "compatible" and compatibility != "Compatible": continue
                    if compatibility_filter == "incompatible" and compatibility != "Incompatible": continue
                    if compatibility_filter == "not_checked" and compatibility != "—": continue
                    if status_filter == "Anonymized" and not status.startswith("Anonymized"): continue
                    if status_filter == "Preserved" and status != status_filter: continue
                    if status_filter == "Unconfigured" and status != status_filter: continue
                    if status_filter == "Needs attention" and status not in {"Table not in source", "Column not in source", "Conflict: preserve + anonymize", "Unconfigured"} and compatible: continue
                    tooltip = reason if not compatible else ""
                    item = QTreeWidgetItem([table, col, source_type or "—", output_type, compatibility, status]); item.setToolTip(4, tooltip); self.rule_tree.addTopLevelItem(item)
            self._update_rule_compatibility_notice()
        except (json.JSONDecodeError, AttributeError, TypeError) as exc:
            if hasattr(self, "rule_tree"): self.rule_tree.clear()

    def load_rule_selection(self) -> None:
        selected = self.rule_tree.selectedItems()
        if not selected:
            self._update_rule_type_filter()
            self._update_rule_compatibility_notice()
            return
        table, col = selected[0].text(0), selected[0].text(1)
        try:
            text = self.rules_text.toPlainText()
            rules = json.loads(text or "{}"); spec = rules.get("tables", {}).get(table, {})
            selected_type = ""
            if col in spec.get("anonymize", {}):
                transform = spec["anonymize"][col]; selected_type = transform.get("type", "email"); self.rule_mode.setCurrentText("Anonymize")
                options = {key: value for key, value in transform.items() if key != "type"}
                self.rule_constant.setText(json.dumps(options.get("value"), ensure_ascii=False) if transform.get("type") == "constant" and "value" in options else (json.dumps(options, ensure_ascii=False) if options else ""))
            elif col in spec.get("preserve", []): self.rule_mode.setCurrentText("Preserve")
            else: self.rule_mode.setCurrentText("Unconfigured")
            self._select_rule_json_entry(text, table, col)
            self._update_rule_type_filter(preferred=selected_type or self.rule_type.currentText())
            self._update_rule_compatibility_notice()
        except Exception: pass

    def show_rule_context_menu(self, position) -> None:
        """Offer source table browsing from a rule row."""
        item = self.rule_tree.itemAt(position)
        if item is None:
            return
        self.rule_tree.setCurrentItem(item)
        table = item.text(0)
        menu = QMenu(self.rule_tree)
        browse = menu.addAction(f"Browse source data for {table}")
        if menu.exec(self.rule_tree.viewport().mapToGlobal(position)) == browse:
            self.browse_source_table(table)

    def update_rule_options_placeholder(self, kind: str) -> None:
        if kind == "constant":
            self.rule_constant.setPlaceholderText("JSON value for constant")
        elif kind == "coordinate_round":
            self.rule_constant.setPlaceholderText('JSON options, e.g. {"precision":2}')
        elif kind == "password":
            self.rule_constant.setPlaceholderText("No options; replaces the password with an unusable marker")
        elif kind in {"passport", "location_generalization", "numeric_variance", "date_round"}:
            self.rule_constant.setPlaceholderText('JSON options, e.g. {"target":"hotel_class"}')
        else:
            self.rule_constant.setPlaceholderText("Optional JSON options")

    def browse_source_table(self, table: str) -> None:
        """Open Browse data on a source table and load its sample."""
        self.tabs.setCurrentWidget(self.browse_tab)
        self.browse_role.setCurrentText("source")
        self.browse_filter.clear()
        self.browse_search_all.clear()
        self.browse_column_value.clear()
        if not any(entry.get("name") == table for entry in self.browse_tables.get("source", [])):
            self._pending_browse_table = table
            self.load_browse_tables()
            return
        self.filter_browse_tables()
        self.browse_table.setCurrentText(table)
        self.search_browse()

    def _select_rule_json_entry(self, text: str, table: str, column: str) -> None:
        """Highlight the selected table/column entry in the JSON rules editor."""
        tables_key = re.search(r'"tables"\s*:', text)
        if not tables_key:
            return
        table_pattern = re.compile(re.escape(json.dumps(table, ensure_ascii=False)) + r"\s*:")
        table_match = table_pattern.search(text, tables_key.end())
        if not table_match:
            return
        opening = text.find("{", table_match.end())
        if opening < 0:
            return
        depth, in_string, escaped = 0, False, False
        closing = None
        for index in range(opening, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    closing = index + 1
                    break
        if closing is None:
            return
        column_token = json.dumps(column, ensure_ascii=False)
        position = text.find(column_token, opening, closing)
        if position < 0:
            return
        cursor = self.rules_text.textCursor()
        cursor.setPosition(position)
        cursor.setPosition(position + len(column_token), cursor.MoveMode.KeepAnchor)
        self.rules_text.setTextCursor(cursor)
        # Scroll using the middle of the selected token. Qt otherwise tends to
        # keep the cursor at the end of the selection visible, which can leave
        # the highlighted start clipped at the top or bottom of the viewport.
        focus = self.rules_text.textCursor()
        focus.setPosition(position + len(column_token) // 2)
        self.rules_text.setTextCursor(focus)
        self.rules_text.ensureCursorVisible()
        self.rules_text.setTextCursor(cursor)
        # The editor can still be laying out after a tree selection, so repeat
        # the visibility adjustment once the current event has completed.
        QTimer.singleShot(0, lambda: self._ensure_rule_selection_visible(position, len(column_token)))

    def _ensure_rule_selection_visible(self, start: int, length: int) -> None:
        cursor = self.rules_text.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(start + length, cursor.MoveMode.KeepAnchor)
        focus = self.rules_text.textCursor()
        focus.setPosition(start + length // 2)
        self.rules_text.setTextCursor(focus)
        self.rules_text.ensureCursorVisible()
        self.rules_text.setTextCursor(cursor)

    def apply_rule(self) -> None:
        selected = self.rule_tree.selectedItems()
        if not selected: return self.info("Select fields", "Select one or more fields in the rule list first.")
        try:
            rules = json.loads(self.rules_text.toPlainText() or "{}")
            mode = self.rule_mode.currentText()
            transform = None
            if mode == "Anonymize":
                if not self.rule_type.currentText():
                    return self.info("Select a compatible transformation", "Choose an anonymization type compatible with every selected field, or turn off the compatibility filter.")
                transform = {"type": self.rule_type.currentText()}
                if transform["type"] == "constant":
                    raw = self.rule_constant.text()
                    try: transform["value"] = json.loads(raw)
                    except json.JSONDecodeError: transform["value"] = raw
                elif self.rule_constant.text().strip():
                    options = json.loads(self.rule_constant.text())
                    if not isinstance(options, dict):
                        raise ValueError("Transformation options must be a JSON object")
                    transform.update(options)
            for item in selected:
                table, col = item.text(0), item.text(1)
                spec = rules.setdefault("tables", {}).setdefault(table, {})
                spec.setdefault("preserve", []); spec.setdefault("anonymize", {})
                spec["preserve"] = [x for x in spec["preserve"] if x != col]
                spec["anonymize"].pop(col, None)
                if mode == "Preserve": spec["preserve"].append(col)
                elif mode == "Anonymize": spec["anonymize"][col] = copy.deepcopy(transform)
            self.rules_text.blockSignals(True); self.rules_text.setPlainText(json.dumps(rules, indent=2, ensure_ascii=False)); self.rules_text.blockSignals(False); self._update_save_rules_button(); self.refresh_rules()
        except Exception as exc: self.error("Could not update rule", exc)

    def remove_rule(self) -> None:
        self.rule_mode.setCurrentText("Unconfigured"); self.apply_rule()

    # Mapping store lookup
    def load_lookup_fields(self) -> None:
        path = self.mapping_edit.text().strip()
        if not path: return self.info("Select mapping store", "Choose a mapping store first.")
        self.run_task("Loading mapping fields…", self._lookup_fields_worker, path)

    @staticmethod
    def _lookup_fields_worker(path: str, progress: Callable) -> tuple: return ("lookup_fields", list_mapping_fields(path))

    def filter_lookup_tables(self) -> None:
        query = getattr(self, "lookup_table_filter", None).text().casefold() if hasattr(self, "lookup_table_filter") else ""
        values = [t for t in self.mapping_fields if query in t.casefold()]
        current = self.lookup_table.currentText(); self.lookup_table.clear(); self.lookup_table.addItems(values)
        if current in values: self.lookup_table.setCurrentText(current)
        self.filter_lookup_columns()

    def filter_lookup_columns(self, *_args) -> None:
        if not hasattr(self, "lookup_column"): return
        query = self.lookup_column_filter.text().casefold(); values = [c for c in self.mapping_fields.get(self.lookup_table.currentText(), []) if query in c.casefold()]
        current = self.lookup_column.currentText(); self.lookup_column.clear(); self.lookup_column.addItems(values)
        if current in values: self.lookup_column.setCurrentText(current)

    def reset_lookup(self) -> None:
        self.lookup_table_filter.clear(); self.lookup_column_filter.clear(); self.lookup_value.clear()

    def copy_browse_cell_for_lookup(self) -> None:
        row, column = self.browse_view.currentRow(), self.browse_view.currentColumn()
        item = self.browse_view.item(row, column) if row >= 0 and column >= 0 else None
        table = self.browse_table.currentText()
        if item is None or not table:
            return self.info("Select a data cell", "Select a cell in Browse data first.")
        payload = {
            "_mysql_anonymizer_lookup": 1,
            "table": table,
            "column": self.browse_view.horizontalHeaderItem(column).text(),
            "value": item.text(),
        }
        QApplication.clipboard().setText(json.dumps(payload, ensure_ascii=False))
        self.status.setText(f"Copied {table}.{payload['column']} for Lookup.")

    def _accept_browse_cell_payload(self, text: str) -> None:
        """Expand a Browse cell clipboard payload pasted into the Lookup value field."""
        try:
            payload = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict) or payload.get("_mysql_anonymizer_lookup") != 1:
            return
        table, column, value = payload.get("table"), payload.get("column"), payload.get("value")
        if not all(isinstance(part, str) for part in (table, column, value)):
            return
        self._select_lookup_field(table, column)
        self.lookup_value.setText(value)

    def _select_lookup_field(self, table: str, column: str) -> None:
        """Select a table and column, updating filters when those fields are available."""
        if table not in self.mapping_fields:
            self._pending_lookup_selection = (table, column)
            self.load_lookup_fields()
            return
        self.lookup_table_filter.clear()
        self.lookup_table.setCurrentText(table)
        if column in self.mapping_fields.get(table, []):
            self.lookup_column_filter.clear()
            self.lookup_column.setCurrentText(column)

    def lookup(self) -> None:
        path, table, column = self.mapping_edit.text().strip(), self.lookup_table.currentText(), self.lookup_column.currentText()
        if not path or not table or not column: return self.info("Complete lookup fields", "Select a mapping store, table, and field.")
        self.run_task("Searching mappings…", self._lookup_worker, path, table, column, self.lookup_value.text(), self.lookup_direction.currentData())

    @staticmethod
    def _lookup_worker(path: str, table: str, column: str, value: str, direction: str, progress: Callable) -> tuple:
        return ("lookup", direction, table, column, value, lookup_mapping(path, table, column, value, direction))

    # Source/destination browsing
    def load_browse_tables(self) -> None:
        try: config = self.config()
        except Exception as exc: return self.error("Invalid connection settings", exc)
        role = self.browse_role.currentText()
        self._launch_database_operation(
            "browse_tables", {"role": role, "settings": config[role]},
            f"Loading {role} tables…",
        )

    def filter_browse_tables(self, *_args) -> None:
        if not hasattr(self, "browse_table"): return
        role = self.browse_role.currentText(); query = self.browse_filter.text().casefold()
        names = [t["name"] for t in self.browse_tables.get(role, []) if query in t["name"].casefold()]
        current = self.browse_table.currentText(); self.browse_table.blockSignals(True); self.browse_table.clear(); self.browse_table.addItems(names)
        if current in names: self.browse_table.setCurrentText(current)
        self.browse_table.blockSignals(False)
        item = next((t for t in self.browse_tables.get(role, []) if t["name"] == self.browse_table.currentText()), None)
        self._set_browse_columns(item)

    def _set_browse_columns(self, item: dict[str, Any] | None) -> None:
        selected = self.browse_column.checked_data()
        self.browse_column.blockSignals(True)
        self.browse_column.model().clear()
        self.browse_column.add_checkable_item("All columns", None, not selected)
        if item:
            for column in item["columns"]:
                name = column["COLUMN_NAME"]
                self.browse_column.add_checkable_item(name, name, name in selected)
        self.browse_column.blockSignals(False)

    def _browse_table_changed(self, table: str) -> None:
        role = self.browse_role.currentText()
        item = next((t for t in self.browse_tables.get(role, []) if t["name"] == table), None)
        self._set_browse_columns(item)
        self.search_browse()

    def reset_browse_column_filter(self) -> None:
        self.browse_column.set_checked_data([])
        self.browse_column_value.clear()
        self.search_browse()

    def search_browse(self, *_args) -> None:
        table = self.browse_table.currentText(); role = self.browse_role.currentText()
        item = next((t for t in self.browse_tables.get(role, []) if t["name"] == table), None)
        if not item: return
        try: config = self.config()
        except Exception as exc: return self.error("Invalid connection settings", exc)
        columns = [c["COLUMN_NAME"] for c in item["columns"]]
        self._launch_database_operation(
            "browse_rows",
            {"role": role, "settings": config[role], "table": table,
             "columns": columns, "search": self.browse_search_all.text(),
             "filter_columns": self.browse_column.checked_data(),
             "filter_value": self.browse_column_value.text()},
            f"Reading {role}.{table}…",
        )

    def show_browse_rows(self, table: str, columns: list[str], rows: list[tuple]) -> None:
        self.browse_view.clear(); self.browse_view.setColumnCount(len(columns)); self.browse_view.setHorizontalHeaderLabels(columns); self.browse_view.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                text = "NULL" if value is None else str(value)
                self.browse_view.setItem(r, c, QTableWidgetItem(text))
        self.browse_view.resizeColumnsToContents()

    def closeEvent(self, event) -> None:
        if self.pause_event is not None and self.copy_resume:
            self.close_when_paused = True
            self.pause_event.set()
            if self._copy_worker_paths is not None:
                self._copy_worker_paths["pause_path"].touch(mode=0o600, exist_ok=True)
            event.ignore(); self.status.setText("Pausing at the next batch boundary before closing…")
            return
        if self.busy:
            self.close_when_paused = True; event.ignore(); self.status.setText("Waiting for the current operation before closing…")
            return
        try: self.save_state()
        except OSError as exc: self.log_message(f"Could not save app state: {exc}")
        event.accept()


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("MySQL Anonymized Copy")
    window = AnonymizerWindow()
    try:
        window._write_activity_log(f"application_started python={sys.version_info.major}.{sys.version_info.minor}")
    except OSError:
        pass
    log_path = window._error_log_path()
    try:
        log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try: log_path.parent.chmod(0o700)
        except OSError: pass
        crash_log = log_path.open("a", encoding="utf-8", buffering=1)
        os.chmod(log_path, 0o600)
        # Keep the descriptor alive for faulthandler so native crashes also leave
        # Python thread stacks in the same private diagnostic log. Route stderr
        # there too because CPython fatal errors write their report directly to
        # file descriptor 2 instead of calling sys.excepthook.
        os.dup2(crash_log.fileno(), 2)
        faulthandler.enable(file=crash_log, all_threads=True)
    except (OSError, RuntimeError, ValueError):
        crash_log = None

    def handle_uncaught(exc_type, exc_value, exc_tb) -> None:
        message = str(exc_value) or exc_type.__name__
        details = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            window._write_error_log("uncaught GUI exception", message, details)
        except OSError:
            print(details, file=sys.__stderr__)
        try:
            window.log_message(f"Unexpected application error: {message}")
            window.log_message(f"Diagnostic log: {log_path}")
            if window.pause_event is not None:
                window.pause_event.set()
                if window._copy_worker_paths is not None:
                    window._copy_worker_paths["pause_path"].touch(mode=0o600, exist_ok=True)
            QTimer.singleShot(0, lambda: window.error(
                "Unexpected application error",
                f"The operation was stopped at a safe batch boundary where possible. Diagnostics were saved to:\n{log_path}\n\n{message}"
            ))
        except Exception:
            print(details, file=sys.__stderr__)

    sys.excepthook = handle_uncaught
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
