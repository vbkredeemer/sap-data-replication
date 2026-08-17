#!/usr/bin/env python3
"""
SAP Data Replication Client — GUI Application
==============================================
Professional desktop client for SAP data replication.

Features:
  - Four replication modes: CDC, Timeframe, Full-Load, Flatfile
  - Per-table configuration with visual editor
  - Connection settings for SAP (RFC), SQL Server, SSH
  - Real-time log output during sync
  - CDC init / remove controls
  - Config persistence (JSON)
  - Status indicators per table

Framework: PySide6 (Qt6, LGPL)
"""

import copy
import html as html_module
import sys
import json
import os
import logging
import time
import subprocess
from datetime import datetime
from typing import Optional

# NWRFC DLL bootstrap — must run before sap_rfc is imported anywhere
from nwrfc_bootstrap import bootstrap as _nwrfc_bootstrap
_nwrfc_status, _nwrfc_msg = _nwrfc_bootstrap()
if _nwrfc_status == "missing":
    sys.exit(1)
if _nwrfc_status == "elevated":
    sys.exit(0)

from PySide6.QtCore import Qt, QThread, Signal, QObject, QSize, QTimer
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QLabel, QLineEdit, QComboBox, QPushButton, QTableWidget,
    QTableWidgetItem, QHeaderView, QGroupBox, QFormLayout, QCheckBox,
    QSpinBox, QMessageBox, QFileDialog, QTextEdit, QProgressBar,
    QStatusBar, QMenu, QAbstractItemView, QDialog, QDialogButtonBox,
    QGridLayout
)
from PySide6.QtGui import QFont, QColor, QAction


# ============================================================================
# Config Model
# ============================================================================

DEFAULT_CONFIG = {
    "sap": {
        "ashost": "",
        "sysnr": "00",
        "client": "100",
        "user": "",
        "password": "",
        "lang": "EN"
    },
    "sql_server": {
        "connection_string": "Driver={ODBC Driver 17 for SQL Server};Server=localhost;Database=SAP_REPL;Trusted_Connection=yes;"
    },
    "ssh": {
        "host": "",
        "user": "",
        "key_file": "",
        "port": 22
    },
    "flatfile": {
        "transfer_method": "scp",
        "smb_share": ""
    },
    "tables": [],
    "schedules": []
}

TABLE_MODES = ["cdc", "timeframe", "full", "flatfile"]
WINDOW_OPTIONS = ["day", "week", "month", "year", "all"]
REPLACE_MODES = ["append", "replace_all", "replace_window"]
SCHEDULE_INTERVALS = ["hourly", "every2h", "every4h", "every6h", "daily", "weekly"]


class ConfigManager:
    """Manages configuration persistence."""

    def __init__(self, config_path: str = None):
        if config_path is None:
            app_dir = os.path.dirname(os.path.abspath(__file__))
            config_path = os.path.join(app_dir, "config.json")
        self.config_path = config_path
        self.config = copy.deepcopy(DEFAULT_CONFIG)

    def load(self) -> dict:
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                # Merge with defaults
                for key in DEFAULT_CONFIG:
                    if key not in loaded:
                        loaded[key] = DEFAULT_CONFIG[key]
                self.config = loaded
            except Exception as e:
                logging.error(f"Cannot load config: {e}")
                self.config = copy.deepcopy(DEFAULT_CONFIG)
        return self.config

    def save(self):
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=2)
        except Exception as e:
            logging.error(f"Cannot save config: {e}")
            raise

    def get(self) -> dict:
        return self.config

    def set(self, config: dict):
        self.config = config
        self.save()


# ============================================================================
# Log Handler — routes log messages to GUI
# ============================================================================

class GuiLogHandler(logging.Handler, QObject):
    """Logging handler that emits signals for GUI display."""
    log_signal = Signal(str, str)  # (level, message)

    def __init__(self):
        logging.Handler.__init__(self)
        QObject.__init__(self)

    def emit(self, record):
        msg = self.format(record)
        self.log_signal.emit(record.levelname, msg)


# ============================================================================
# Sync Worker Thread
# ============================================================================

class SyncWorker(QThread):
    """Runs replication in background thread."""
    progress = Signal(str, str)  # (table_name, status)
    finished_all = Signal(int, int)  # (success_count, fail_count)
    log_message = Signal(str, str)  # (level, message)

    def __init__(self, config: dict, tables_to_sync: list, action: str = "sync"):
        super().__init__()
        self.config = config
        self.tables_to_sync = tables_to_sync
        self.action = action  # "sync", "init_only", "remove_cdc"
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        # Setup logging to emit to GUI — attach to sap_replicate logger only
        handler = GuiLogHandler()
        handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', '%H:%M:%S'))
        handler.log_signal = self.log_message
        sap_logger = logging.getLogger('sap_replicate')
        sap_logger.addHandler(handler)
        sap_logger.setLevel(logging.INFO)

        success = 0
        fail = 0

        try:
            # Import replication logic
            p = os.path.dirname(os.path.abspath(__file__))
            if p not in sys.path: sys.path.insert(0, p)
            from sap_replicate import (
                SapConnection, SqlServerConnection, StateManager,
                CdcReplicator, TimeframeReplicator, FullLoadReplicator,
                FlatfileReplicator, SchemaManager, run_table
            )

            # Connect
            sap = SapConnection(self.config['sap'])
            sql = SqlServerConnection(self.config['sql_server'])
            state = StateManager(sql)

            try:
                sap.connect()
                sql.connect()

                if self.action == "init_only":
                    for t in self.tables_to_sync:
                        if self._cancel:
                            break
                        try:
                            if t.get('mode') == 'cdc' and t.get('key_fields'):
                                cdc = CdcReplicator(sap, sql, state)
                                cdc.init_table(t['name'], t['key_fields'])
                                self.progress.emit(t['name'], "Init OK")
                                success += 1
                        except Exception as e:
                            logging.error(f"Init error for {t['name']}: {e}")
                            fail += 1
                            self.progress.emit(t['name'], "✗ Error")
                    return

                if self.action == "remove_cdc":
                    for t in self.tables_to_sync:
                        if self._cancel:
                            break
                        try:
                            cdc = CdcReplicator(sap, sql, state)
                            cdc.remove_cdc(t['name'])
                            self.progress.emit(t['name'], "Removed")
                            success += 1
                        except Exception as e:
                            logging.error(f"Remove CDC error for {t['name']}: {e}")
                            fail += 1
                            self.progress.emit(t['name'], "✗ Error")
                    return

                if self.action == "sync_schema":
                    schema = SchemaManager(sap, sql)
                    for t in self.tables_to_sync:
                        if self._cancel:
                            break
                        target = t.get('target_table') or t['name']
                        self.progress.emit(t['name'], "Creating...")
                        try:
                            ok = schema.sync_schema(t['name'], target, drop_if_exists=True)
                            if ok:
                                success += 1
                                self.progress.emit(t['name'], "✓ Schema created")
                            else:
                                fail += 1
                                self.progress.emit(t['name'], "✗ Schema failed")
                        except Exception as e:
                            logging.error(f"Schema error for {t['name']}: {e}")
                            fail += 1
                            self.progress.emit(t['name'], "✗ Error")
                    return

                # Normal sync
                for t in self.tables_to_sync:
                    if self._cancel:
                        logging.info("Sync cancelled by user")
                        break

                    self.progress.emit(t['name'], "Running...")
                    try:
                        ok = run_table(t, sap, sql, state, self.config)
                        if ok:
                            success += 1
                            self.progress.emit(t['name'], "✓ Success")
                        else:
                            fail += 1
                            self.progress.emit(t['name'], "✗ Failed")
                    except Exception as e:
                        logging.error(f"{t['name']}: {e}")
                        fail += 1
                        self.progress.emit(t['name'], "✗ Error")

            finally:
                try:
                    sap.close()
                except Exception:
                    pass
                try:
                    sql.close()
                except Exception:
                    pass

        except Exception as e:
            logging.error(f"Fatal error: {e}")
            fail += 1

        finally:
            try:
                sap_logger.removeHandler(handler)
            except Exception:
                pass
            self.finished_all.emit(success, fail)


# ============================================================================
# Settings Tab
# ============================================================================

class SettingsTab(QWidget):
    """Connection settings for SAP, SQL Server, SSH."""

    config_changed = Signal()

    def __init__(self, config_manager: ConfigManager):
        super().__init__()
        self.config_manager = config_manager
        self.config = config_manager.get()
        self._build_ui()
        self._load_values()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # --- SAP Connection ---
        sap_group = QGroupBox("SAP-Verbindung (RFC)")
        sap_form = QFormLayout()

        self.sap_host = QLineEdit()
        self.sap_sysnr = QLineEdit()
        self.sap_sysnr.setMaximumWidth(60)
        self.sap_client = QLineEdit()
        self.sap_client.setMaximumWidth(60)
        self.sap_user = QLineEdit()
        self.sap_password = QLineEdit()
        self.sap_password.setEchoMode(QLineEdit.Password)
        self.sap_lang = QLineEdit()
        self.sap_lang.setMaximumWidth(40)

        sap_form.addRow("Application Server:", self.sap_host)
        sap_form.addRow("Systemnummer:", self.sap_sysnr)
        sap_form.addRow("Mandant:", self.sap_client)
        sap_form.addRow("User:", self.sap_user)
        sap_form.addRow("Passwort:", self.sap_password)
        sap_form.addRow("Sprache:", self.sap_lang)
        sap_group.setLayout(sap_form)
        layout.addWidget(sap_group)

        # --- SQL Server Connection ---
        sql_group = QGroupBox("SQL Server Verbindung")
        sql_form = QFormLayout()

        self.sql_connstr = QLineEdit()
        self.sql_connstr.setPlaceholderText("Driver={ODBC Driver 17 for SQL Server};Server=localhost;Database=SAP_REPL;Trusted_Connection=yes;")
        sql_form.addRow("Connection String:", self.sql_connstr)

        sql_group.setLayout(sql_form)
        layout.addWidget(sql_group)

        # --- SSH Connection (for Flatfile mode) ---
        ssh_group = QGroupBox("SSH-Verbindung (für Flatfile-Modus)")
        ssh_form = QFormLayout()

        self.ssh_host = QLineEdit()
        self.ssh_user = QLineEdit()
        self.ssh_key = QLineEdit()
        self.ssh_port = QSpinBox()
        self.ssh_port.setRange(1, 65535)
        self.ssh_port.setValue(22)

        ssh_browse = QPushButton("...")
        ssh_browse.setMaximumWidth(30)
        ssh_browse.clicked.connect(self._browse_key_file)

        key_layout = QHBoxLayout()
        key_layout.addWidget(self.ssh_key)
        key_layout.addWidget(ssh_browse)

        ssh_form.addRow("Host:", self.ssh_host)
        ssh_form.addRow("User:", self.ssh_user)
        ssh_form.addRow("Key File:", key_layout)
        ssh_form.addRow("Port:", self.ssh_port)
        ssh_group.setLayout(ssh_form)
        layout.addWidget(ssh_group)

        # --- Flatfile Transfer Method ---
        ff_group = QGroupBox("Flatfile-Übertragung")
        ff_form = QFormLayout()

        self.ff_method = QComboBox()
        self.ff_method.addItems(['scp', 'smb', 'local'])
        self.ff_method.currentTextChanged.connect(self._on_method_changed)
        ff_form.addRow("Übertragungsmethode:", self.ff_method)

        self.ff_smb_share = QLineEdit()
        self.ff_smb_share.setPlaceholderText(r"\\sap-server\sap\tmp")
        ff_form.addRow("SMB-Share (UNC-Pfad):", self.ff_smb_share)

        ff_group.setLayout(ff_form)
        layout.addWidget(ff_group)

        # --- Buttons ---
        btn_layout = QHBoxLayout()
        self.btn_save = QPushButton("Speichern")
        self.btn_save.clicked.connect(self._save)
        self.btn_test_sap = QPushButton("SAP testen")
        self.btn_test_sap.clicked.connect(self._test_sap)
        self.btn_test_sql = QPushButton("SQL Server testen")
        self.btn_test_sql.clicked.connect(self._test_sql)
        btn_layout.addWidget(self.btn_save)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_test_sap)
        btn_layout.addWidget(self.btn_test_sql)
        layout.addLayout(btn_layout)

        layout.addStretch()

    def _browse_key_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "SSH Key File", "", "All Files (*.*)")
        if path:
            self.ssh_key.setText(path)

    def _on_method_changed(self, method: str):
        """Enable/disable SMB share field based on transfer method."""
        self.ff_smb_share.setEnabled(method == 'smb')
        if method == 'scp':
            self.ff_smb_share.setPlaceholderText("(nicht verwendet bei SCP)")
        elif method == 'smb':
            self.ff_smb_share.setPlaceholderText(r"\\sap-server\sap\tmp")
        elif method == 'local':
            self.ff_smb_share.setPlaceholderText("(nicht verwendet bei Local)")

    def _load_values(self):
        sap = self.config.get('sap', {})
        self.sap_host.setText(sap.get('ashost', ''))
        self.sap_sysnr.setText(sap.get('sysnr', '00'))
        self.sap_client.setText(sap.get('client', '100'))
        self.sap_user.setText(sap.get('user', ''))
        self.sap_password.setText(sap.get('password', ''))
        self.sap_lang.setText(sap.get('lang', 'EN'))

        sql = self.config.get('sql_server', {})
        self.sql_connstr.setText(sql.get('connection_string', ''))

        ssh = self.config.get('ssh', {})
        self.ssh_host.setText(ssh.get('host', ''))
        self.ssh_user.setText(ssh.get('user', ''))
        self.ssh_key.setText(ssh.get('key_file', ''))
        self.ssh_port.setValue(int(ssh.get('port', 22)))

        ff = self.config.get('flatfile', {})
        self.ff_method.setCurrentText(ff.get('transfer_method', 'scp'))
        self.ff_smb_share.setText(ff.get('smb_share', ''))
        self._on_method_changed(self.ff_method.currentText())

    def _save(self):
        ok, msg = self._save_silent()
        if ok:
            QMessageBox.information(self, "Gespeichert", msg)
            self.config_changed.emit()
        else:
            QMessageBox.critical(self, "Fehler", msg)

    def _save_silent(self):
        """Save settings without showing a dialog. Returns (success, message)."""
        self.config['sap'] = {
            'ashost': self.sap_host.text(),
            'sysnr': self.sap_sysnr.text(),
            'client': self.sap_client.text(),
            'user': self.sap_user.text(),
            'password': self.sap_password.text(),
            'lang': self.sap_lang.text() or 'EN'
        }
        self.config['sql_server'] = {
            'connection_string': self.sql_connstr.text()
        }
        self.config['ssh'] = {
            'host': self.ssh_host.text(),
            'user': self.ssh_user.text(),
            'key_file': self.ssh_key.text(),
            'port': self.ssh_port.value()
        }
        self.config['flatfile'] = {
            'transfer_method': self.ff_method.currentText(),
            'smb_share': self.ff_smb_share.text()
        }
        try:
            self.config_manager.set(self.config)
            return True, "Konfiguration erfolgreich gespeichert."
        except Exception as e:
            return False, f"Speichern fehlgeschlagen: {e}"

    def _test_sap(self):
        try:
            p = os.path.dirname(os.path.abspath(__file__))
            if p not in sys.path: sys.path.insert(0, p)
            from sap_rfc import Connection
            conn = Connection(
                ashost=self.sap_host.text(),
                sysnr=self.sap_sysnr.text(),
                client=self.sap_client.text(),
                user=self.sap_user.text(),
                passwd=self.sap_password.text(),
                lang=self.sap_lang.text() or 'EN'
            )
            conn.close()
            QMessageBox.information(self, "Erfolg", "SAP-Verbindung erfolgreich.")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"SAP-Verbindung fehlgeschlagen:\n{e}")

    def _test_sql(self):
        try:
            import pyodbc
            conn = pyodbc.connect(self.sql_connstr.text(), timeout=5)
            conn.close()
            QMessageBox.information(self, "Erfolg", "SQL Server Verbindung erfolgreich.")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"SQL Server Verbindung fehlgeschlagen:\n{e}")


# ============================================================================
# Tables Tab
# ============================================================================

class TableDetailDialog(QDialog):
    """Detail dialog for editing ALL fields of a single table config.

    Takes a table config dict as input, returns the modified dict via
    get_config() or None if cancelled. Modal.
    """

    def __init__(self, table_config: dict, parent=None):
        super().__init__(parent)
        self._config = copy.deepcopy(table_config)
        name = table_config.get('name', '')
        self.setWindowTitle(f"Tabelle bearbeiten: {name}" if name else "Neue Tabelle")
        self.setModal(True)
        self.setMinimumWidth(420)
        self._build_ui()
        self._load_values()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)

        # name
        self.name_edit = QLineEdit()
        form.addRow("Tabelle:", self.name_edit)

        # mode
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(TABLE_MODES)
        form.addRow("Modus:", self.mode_combo)

        # active
        self.active_check = QCheckBox("aktiv")
        form.addRow("Aktiv:", self.active_check)

        # key_fields
        self.key_fields_edit = QLineEdit()
        self.key_fields_edit.setPlaceholderText("z.B. MANDT,MATNR")
        form.addRow("Key Fields:", self.key_fields_edit)

        # delta_field
        self.delta_field_edit = QLineEdit()
        self.delta_field_edit.setPlaceholderText("z.B. AEDAT, LAEDA")
        form.addRow("Delta Field:", self.delta_field_edit)

        # window
        self.window_combo = QComboBox()
        self.window_combo.addItems([''] + WINDOW_OPTIONS)
        form.addRow("Window:", self.window_combo)

        # target_table
        self.target_table_edit = QLineEdit()
        self.target_table_edit.setPlaceholderText("(leer = gleicher Name)")
        form.addRow("Target Table:", self.target_table_edit)

        # replace_mode
        self.replace_combo = QComboBox()
        self.replace_combo.addItems(REPLACE_MODES)
        form.addRow("Replace Mode:", self.replace_combo)

        # chunk_size
        self.chunk_spin = QSpinBox()
        self.chunk_spin.setRange(1, 1000000)
        self.chunk_spin.setSingleStep(1000)
        form.addRow("Chunk Size:", self.chunk_spin)

        # fields
        self.fields_edit = QLineEdit()
        self.fields_edit.setPlaceholderText("* oder MATNR,ERNAM,...")
        form.addRow("Fields:", self.fields_edit)

        # file_path
        self.file_path_edit = QLineEdit()
        self.file_path_edit.setPlaceholderText("/usr/sap/tmp/")
        form.addRow("File Path:", self.file_path_edit)

        # date_field
        self.date_field_edit = QLineEdit()
        self.date_field_edit.setPlaceholderText("(optional)")
        form.addRow("Date Field:", self.date_field_edit)

        # max_rows
        self.max_rows_spin = QSpinBox()
        self.max_rows_spin.setRange(0, 10000000)
        self.max_rows_spin.setSingleStep(1000)
        self.max_rows_spin.setSpecialValueText("0 (unbegrenzt)")
        form.addRow("Max Rows:", self.max_rows_spin)

        layout.addLayout(form)

        # Buttons
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _load_values(self):
        t = self._config
        self.name_edit.setText(t.get('name', ''))
        self.mode_combo.setCurrentText(t.get('mode', 'timeframe'))
        self.active_check.setChecked(t.get('active', True))
        self.key_fields_edit.setText(t.get('key_fields', ''))
        self.delta_field_edit.setText(t.get('delta_field', ''))
        self.window_combo.setCurrentText(t.get('window', '') or '')
        self.target_table_edit.setText(t.get('target_table', '') or '')
        self.replace_combo.setCurrentText(t.get('replace_mode', 'append'))
        self.chunk_spin.setValue(t.get('chunk_size', 10000))
        self.fields_edit.setText(t.get('fields') or '*')
        self.file_path_edit.setText(t.get('file_path', '') or '')
        self.date_field_edit.setText(t.get('date_field', '') or '')
        self.max_rows_spin.setValue(t.get('max_rows', 0))

    def get_config(self) -> dict:
        """Return the modified config dict. Call after exec() returns Accepted."""
        cfg = copy.deepcopy(self._config)
        cfg['name'] = self.name_edit.text().strip()
        cfg['mode'] = self.mode_combo.currentText()
        cfg['active'] = self.active_check.isChecked()
        cfg['key_fields'] = self.key_fields_edit.text().strip()
        cfg['delta_field'] = self.delta_field_edit.text().strip()
        cfg['window'] = self.window_combo.currentText() or None
        tt = self.target_table_edit.text().strip()
        cfg['target_table'] = tt or None
        cfg['replace_mode'] = self.replace_combo.currentText()
        cfg['chunk_size'] = self.chunk_spin.value()
        cfg['fields'] = self.fields_edit.text().strip() or '*'
        fp = self.file_path_edit.text().strip()
        cfg['file_path'] = fp or None
        df = self.date_field_edit.text().strip()
        cfg['date_field'] = df or None
        cfg['max_rows'] = self.max_rows_spin.value()
        return cfg


# Column indices in the compact overview table
COL_NAME = 0
COL_MODE = 1
COL_KEY = 2
COL_WINDOW = 3
COL_ACTIVE = 4

# Qt.UserRole key for storing the full config dict on the name item
CONFIG_ROLE = Qt.UserRole + 1


class TablesTab(QWidget):
    """Table configuration: compact 5-column overview with detail dialog.

    Stores the full config dict per row in Qt.UserRole on the name item,
    so the detail dialog can read/write the complete config without
    losing fields that are not shown in the overview.
    """

    config_changed = Signal()

    def __init__(self, config_manager: ConfigManager):
        super().__init__()
        self.config_manager = config_manager
        self.config = config_manager.get()
        self._build_ui()
        self._load_tables()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # --- Toolbar ---
        toolbar = QHBoxLayout()
        self.btn_add = QPushButton("+ Tabelle hinzufügen")
        self.btn_add.clicked.connect(self._add_table)
        self.btn_remove = QPushButton("- Entfernen")
        self.btn_remove.clicked.connect(self._remove_table)
        self.btn_import = QPushButton("Import")
        self.btn_import.setToolTip("Tabellen aus Datei importieren (eine pro Zeile, als inaktiv)")
        self.btn_import.clicked.connect(self._import_tables)
        self.btn_edit = QPushButton("Bearbeiten")
        self.btn_edit.setToolTip("Detail-Dialog für ausgewählte Tabelle öffnen")
        self.btn_edit.clicked.connect(self._edit_selected)
        self.btn_save = QPushButton("Speichern")
        self.btn_save.clicked.connect(self._save)
        toolbar.addWidget(self.btn_add)
        toolbar.addWidget(self.btn_remove)
        toolbar.addWidget(self.btn_import)
        toolbar.addWidget(self.btn_edit)
        toolbar.addStretch()
        toolbar.addWidget(self.btn_save)
        layout.addLayout(toolbar)

        # --- Compact overview table (5 columns) ---
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels([
            "Tabelle", "Modus", "Key Fields", "Window", "Aktiv"
        ])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.doubleClicked.connect(self._on_double_click)
        # Column widths
        self.table.setColumnWidth(COL_NAME, 180)
        self.table.setColumnWidth(COL_MODE, 90)
        self.table.setColumnWidth(COL_KEY, 200)
        self.table.setColumnWidth(COL_WINDOW, 90)
        self.table.setColumnWidth(COL_ACTIVE, 60)
        layout.addWidget(self.table)

    def _on_double_click(self, index):
        """Open detail dialog on double-click (same as Bearbeiten button)."""
        self._edit_row(index.row())

    def _edit_selected(self):
        """Open detail dialog for the currently selected row."""
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Bearbeiten",
                "Bitte zuerst eine Tabelle auswählen.")
            return
        self._edit_row(row)

    def _edit_row(self, row: int):
        """Open the detail dialog for a row and apply the result."""
        cfg = self._get_row_config(row)
        dialog = TableDetailDialog(cfg, parent=self)
        if dialog.exec() == QDialog.Accepted:
            new_cfg = dialog.get_config()
            self._set_row(row, new_cfg)

    # ------------------------------------------------------------------
    # Row data helpers
    # ------------------------------------------------------------------

    def _load_tables(self):
        """Load tables from config into the overview widget."""
        self.table.setRowCount(0)
        for t in self.config.get('tables', []):
            row = self.table.rowCount()
            self.table.insertRow(row)
            self._set_row(row, t)

    def _set_row(self, row: int, t: dict):
        """Set the 5 visible columns and store full config in Qt.UserRole."""
        # Name
        name_item = QTableWidgetItem(t.get('name', ''))
        name_item.setData(CONFIG_ROLE, copy.deepcopy(t))
        self.table.setItem(row, COL_NAME, name_item)

        # Mode (read-only text in overview)
        self.table.setItem(row, COL_MODE, QTableWidgetItem(t.get('mode', 'timeframe')))

        # Key Fields
        self.table.setItem(row, COL_KEY, QTableWidgetItem(t.get('key_fields', '')))

        # Window
        self.table.setItem(row, COL_WINDOW, QTableWidgetItem(t.get('window', '') or ''))

        # Active checkbox
        active_check = QCheckBox()
        active_check.setChecked(t.get('active', True))
        self.table.setCellWidget(row, COL_ACTIVE, active_check)

    def _get_row_config(self, row: int) -> dict:
        """Read the full config dict stored on the name item (UserRole)."""
        item = self.table.item(row, COL_NAME)
        if item is not None:
            cfg = item.data(CONFIG_ROLE)
            if cfg is not None:
                # Sync the active checkbox state (user can toggle inline)
                cfg = copy.deepcopy(cfg)
                widget = self.table.cellWidget(row, COL_ACTIVE)
                if isinstance(widget, QCheckBox):
                    cfg['active'] = widget.isChecked()
                return cfg
        # Fallback: build from visible cells
        return {
            'name': self.table.item(row, COL_NAME).text() if self.table.item(row, COL_NAME) else '',
            'mode': self.table.item(row, COL_MODE).text() if self.table.item(row, COL_MODE) else 'timeframe',
            'key_fields': self.table.item(row, COL_KEY).text() if self.table.item(row, COL_KEY) else '',
            'window': self.table.item(row, COL_WINDOW).text() if self.table.item(row, COL_WINDOW) else '',
            'active': True,
        }

    def _add_table(self):
        row = self.table.rowCount()
        self.table.insertRow(row)
        defaults = {
            'name': 'NEW_TABLE',
            'mode': 'timeframe',
            'key_fields': '',
            'delta_field': 'AEDAT',
            'window': 'month',
            'target_table': '',
            'replace_mode': 'append',
            'chunk_size': 10000,
            'fields': '*',
            'file_path': '',
            'date_field': '',
            'max_rows': 0,
            'active': True
        }
        self._set_row(row, defaults)
        # Open the detail dialog immediately for editing
        self.table.selectRow(row)
        self._edit_row(row)

    def _remove_table(self):
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def _import_tables(self):
        """Import table names from a file as inactive entries."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Tabellen-Datei importieren", "",
            "Text Files (*.txt);;All Files (*.*)"
        )
        if not path:
            return

        try:
            with open(path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Datei konnte nicht gelesen werden:\n{e}")
            return

        # Parse table names
        import re
        valid_re = re.compile(r'^[A-Za-z_][A-Za-z0-9_/]{0,30}$')
        new_names = []
        for line in lines:
            name = line.strip()
            if not name or name.startswith('#'):
                continue
            if not valid_re.match(name):
                continue
            new_names.append(name.upper())

        # Deduplicate within file
        seen = set()
        unique_names = []
        for n in new_names:
            if n not in seen:
                seen.add(n)
                unique_names.append(n)

        # Get existing table names (from the overview rows)
        existing = set()
        for i in range(self.table.rowCount()):
            item = self.table.item(i, COL_NAME)
            if item:
                existing.add(item.text().upper())

        added = []
        for name in unique_names:
            if name not in existing:
                added.append(name)

        if not added:
            QMessageBox.information(self, "Import",
                f"Keine neuen Tabellen gefunden.\n"
                f"{len(unique_names)} Tabellen in Datei, alle bereits vorhanden.")
            return

        # Add new rows to the table widget
        for name in added:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self._set_row(row, {
                'name': name,
                'mode': 'full',
                'key_fields': '',
                'delta_field': '',
                'window': '',
                'target_table': '',
                'replace_mode': 'append',
                'chunk_size': 10000,
                'fields': '*',
                'file_path': '',
                'date_field': '',
                'max_rows': 0,
                'active': False
            })

        already_existed = len(unique_names) - len(added)
        QMessageBox.information(self, "Import erfolgreich",
            f"{len(added)} neue Tabellen importiert (als inaktiv).\n"
            f"{already_existed} bereits vorhanden.\n\n"
            f"Neue Tabellen:\n" + "\n".join(f"  {n}" for n in added) +
            f"\n\nSpeichern Sie die Konfiguration, um die Änderungen zu übernehmen.\n"
            f"Aktivieren Sie die Tabellen im 'Aktiv' Checkbox, um sie zu syncen.")

    def _context_menu(self, pos):
        menu = QMenu(self)
        action_edit = menu.addAction("Bearbeiten")
        action_add = menu.addAction("Duplizieren")
        action_del = menu.addAction("Entfernen")
        action = menu.exec_(self.table.mapToGlobal(pos))
        row = self.table.rowAt(pos.y())
        if action == action_edit and row >= 0:
            self._edit_row(row)
        elif action == action_add and row >= 0:
            self._duplicate_row(row)
        elif action == action_del and row >= 0:
            self.table.removeRow(row)

    def _duplicate_row(self, row: int):
        t = self._get_row_config(row)
        t['name'] = (t.get('name', '') or '') + '_COPY'
        new_row = self.table.rowCount()
        self.table.insertRow(new_row)
        self._set_row(new_row, t)

    # ------------------------------------------------------------------
    # Config collection / save
    # ------------------------------------------------------------------

    def _collect_tables(self) -> list:
        """Collect all table configs from the overview rows."""
        tables = []
        for i in range(self.table.rowCount()):
            t = self._get_row_config(i)
            if t.get('name') and t['name'] != 'NEW_TABLE':
                tables.append(t)
        return tables

    def _save(self):
        ok, msg = self._save_silent()
        if ok:
            QMessageBox.information(self, "Gespeichert", msg)
            self.config_changed.emit()
        else:
            QMessageBox.critical(self, "Fehler", msg)

    def _save_silent(self):
        """Save tables without showing a dialog. Returns (success, message)."""
        tables = self._collect_tables()

        # Preserve unknown fields from existing config
        old_by_name = {t.get('name'): t for t in self.config.get('tables', [])}
        for t in tables:
            old = old_by_name.get(t['name'], {})
            for k, v in old.items():
                if k not in t:
                    t[k] = v

        self.config['tables'] = tables
        try:
            self.config_manager.set(self.config)
            return True, f"{len(tables)} Tabellen gespeichert."
        except Exception as e:
            return False, f"Speichern fehlgeschlagen: {e}"

    def get_active_tables(self) -> list:
        return [t for t in self._collect_tables()
                if t.get('active', True) and t.get('name') and t['name'] != 'NEW_TABLE']

    def get_selected_table(self) -> Optional[dict]:
        row = self.table.currentRow()
        if row >= 0:
            return self._get_row_config(row)
        return None


# ============================================================================
# Run Tab
# ============================================================================

class RunTab(QWidget):
    """Run sync operations and monitor progress."""

    def __init__(self, config_manager: ConfigManager, tables_tab: TablesTab):
        super().__init__()
        self.config_manager = config_manager
        self.tables_tab = tables_tab
        self.worker = None
        self._scheduler_triggered = False
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # --- Action Buttons ---
        action_group = QGroupBox("Aktionen")
        action_layout = QHBoxLayout()

        self.btn_sync_all = QPushButton("Alle aktiven Tabellen syncen")
        self.btn_sync_all.clicked.connect(self._sync_all)
        self.btn_sync_selected = QPushButton("Ausgewählte Tabelle syncen")
        self.btn_sync_selected.clicked.connect(self._sync_selected)
        self.btn_init_cdc = QPushButton("CDC initialisieren")
        self.btn_init_cdc.clicked.connect(self._init_cdc)
        self.btn_remove_cdc = QPushButton("CDC entfernen")
        self.btn_remove_cdc.clicked.connect(self._remove_cdc)
        self.btn_cancel = QPushButton("Abbrechen")
        self.btn_cancel.clicked.connect(self._cancel)
        self.btn_cancel.setEnabled(False)

        self.btn_sync_schema = QPushButton("Schema erstellen")
        self.btn_sync_schema.setToolTip("Tabelle + Indizes in MSSQL aus SAP-Metadaten erstellen")
        self.btn_sync_schema.clicked.connect(self._sync_schema)

        self.btn_sync_schema_all = QPushButton("Alle Schemata erstellen")
        self.btn_sync_schema_all.setToolTip("Alle Tabellen + Indizes in MSSQL erstellen")
        self.btn_sync_schema_all.clicked.connect(self._sync_schema_all)

        action_layout.addWidget(self.btn_sync_all)
        action_layout.addWidget(self.btn_sync_selected)
        action_layout.addWidget(self.btn_init_cdc)
        action_layout.addWidget(self.btn_remove_cdc)
        action_layout.addWidget(self.btn_sync_schema)
        action_layout.addWidget(self.btn_sync_schema_all)
        action_layout.addStretch()
        action_layout.addWidget(self.btn_cancel)
        action_group.setLayout(action_layout)
        layout.addWidget(action_group)

        # --- Progress Table ---
        prog_group = QGroupBox("Fortschritt")
        prog_layout = QVBoxLayout()

        self.progress_table = QTableWidget()
        self.progress_table.setColumnCount(3)
        self.progress_table.setHorizontalHeaderLabels(["Tabelle", "Status", "Zeit"])
        self.progress_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.progress_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.progress_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        prog_layout.addWidget(self.progress_table)

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        prog_layout.addWidget(self.progress_bar)

        prog_group.setLayout(prog_layout)
        layout.addWidget(prog_group)

        # --- Log Output ---
        log_group = QGroupBox("Protokoll")
        log_layout = QVBoxLayout()

        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setFont(QFont("Consolas", 9))
        log_layout.addWidget(self.log_output)

        self.btn_clear_log = QPushButton("Protokoll löschen")
        self.btn_clear_log.clicked.connect(self.log_output.clear)
        log_layout.addWidget(self.btn_clear_log)

        log_group.setLayout(log_layout)
        layout.addWidget(log_group)

    def _log(self, level: str, message: str):
        color = {
            'INFO': '#000000',
            'WARNING': '#CC8800',
            'ERROR': '#CC0000',
            'DEBUG': '#888888'
        }.get(level, '#000000')
        self.log_output.append(f'<span style="color:{color}">{html_module.escape(message)}</span>')

    def _set_progress(self, table_name: str, status: str):
        # Find or create row
        for i in range(self.progress_table.rowCount()):
            item0 = self.progress_table.item(i, 0)
            if item0 and item0.text() == table_name:
                item1 = self.progress_table.item(i, 1)
                if item1:
                    item1.setText(status)
                item2 = self.progress_table.item(i, 2)
                if item2:
                    item2.setText(datetime.now().strftime('%H:%M:%S'))
                # Color the status
                if item1:
                    if '✓' in status:
                        item1.setForeground(QColor('#008800'))
                    elif '✗' in status or 'Error' in status:
                        item1.setForeground(QColor('#CC0000'))
                    elif 'Running' in status:
                        item1.setForeground(QColor('#0066CC'))
                return

        row = self.progress_table.rowCount()
        self.progress_table.insertRow(row)
        self.progress_table.setItem(row, 0, QTableWidgetItem(table_name))
        self.progress_table.setItem(row, 1, QTableWidgetItem(status))
        self.progress_table.setItem(row, 2, QTableWidgetItem(datetime.now().strftime('%H:%M:%S')))

    def _start_worker(self, tables: list, action: str = "sync"):
        if self.worker and self.worker.isRunning():
            QMessageBox.warning(self, "Laufend", "Ein Vorgang läuft bereits.")
            return

        if not tables:
            QMessageBox.warning(self, "Keine Tabellen", "Keine Tabellen ausgewählt.")
            return

        # Clear progress table
        self.progress_table.setRowCount(0)

        # Disable buttons
        self._set_buttons_enabled(False)

        # Start worker
        config = self.config_manager.get()
        self.worker = SyncWorker(config, tables, action)
        self.worker.progress.connect(self._set_progress)
        self.worker.log_message.connect(self._log)
        self.worker.finished_all.connect(self._on_finished)
        self.worker.start()

    def _sync_all(self):
        tables = self.tables_tab.get_active_tables()
        self._start_worker(tables, "sync")

    def _sync_selected(self):
        t = self.tables_tab.get_selected_table()
        if t:
            self._start_worker([t], "sync")
        else:
            QMessageBox.warning(self, "Keine Auswahl", "Bitte eine Tabelle in der Tables-Liste auswählen.")

    def _init_cdc(self):
        tables = [t for t in self.tables_tab.get_active_tables()
                  if t.get('mode') == 'cdc' and t.get('key_fields')]
        if not tables:
            QMessageBox.warning(self, "Keine CDC-Tabellen", "Keine aktiven Tabellen mit CDC-Modus und Key Fields.")
            return
        self._start_worker(tables, "init_only")

    def _remove_cdc(self):
        t = self.tables_tab.get_selected_table()
        if not t:
            QMessageBox.warning(self, "Keine Auswahl", "Bitte eine Tabelle auswählen.")
            return

        reply = QMessageBox.question(self, "Bestätigen",
            f"CDC für {t['name']} komplett entfernen?\n(Trigger + Log-Tabelle werden gelöscht)")
        if reply == QMessageBox.Yes:
            self._start_worker([t], "remove_cdc")

    def _sync_schema(self):
        t = self.tables_tab.get_selected_table()
        if not t:
            QMessageBox.warning(self, "Keine Auswahl", "Bitte eine Tabelle auswählen.")
            return
        reply = QMessageBox.question(self, "Bestätigen",
            f"Tabelle dbo.{t.get('target_table') or t['name']} in MSSQL erstellen?\n"
            f"(Bestehende Tabelle wird gelöscht und neu erstellt mit Indizes aus SAP)")
        if reply == QMessageBox.Yes:
            self._start_worker([t], "sync_schema")

    def _sync_schema_all(self):
        tables = self.tables_tab.get_active_tables()
        if not tables:
            QMessageBox.warning(self, "Keine Tabellen", "Keine aktiven Tabellen.")
            return
        reply = QMessageBox.question(self, "Bestätigen",
            f"{len(tables)} Tabellen in MSSQL erstellen?\n"
            f"(Bestehende Tabellen werden gelöscht und neu erstellt mit Indizes)")
        if reply == QMessageBox.Yes:
            self._start_worker(tables, "sync_schema")

    def _cancel(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self._log("WARNING", "Abbruch angefordert...")

    def _set_buttons_enabled(self, enabled: bool):
        self.btn_sync_all.setEnabled(enabled)
        self.btn_sync_selected.setEnabled(enabled)
        self.btn_init_cdc.setEnabled(enabled)
        self.btn_remove_cdc.setEnabled(enabled)
        self.btn_sync_schema.setEnabled(enabled)
        self.btn_sync_schema_all.setEnabled(enabled)
        self.btn_cancel.setEnabled(not enabled)

    def _on_finished(self, success: int, fail: int):
        self._set_buttons_enabled(True)
        self._log("INFO", f"=== Sync abgeschlossen: {success} erfolgreich, {fail} fehlgeschlagen ===")
        # Only show modal dialog for manual syncs, not scheduler-triggered ones
        if not getattr(self, '_scheduler_triggered', False):
            QMessageBox.information(self, "Fertig",
                f"Sync abgeschlossen.\n{success} erfolgreich, {fail} fehlgeschlagen.")
        else:
            self.status_bar.showMessage(
                f"Scheduler-Sync fertig: {success} OK, {fail} fehlgeschlagen", 10000)
        self._scheduler_triggered = False


# ============================================================================
# Schedule Tab — job scheduler + Windows Task export
# ============================================================================

class ScheduleTab(QWidget):
    """Job scheduling: built-in scheduler + Windows Task Scheduler export."""

    def __init__(self, config_manager: ConfigManager,
                 tables_tab: TablesTab, run_tab: RunTab):
        super().__init__()
        self.config_manager = config_manager
        self.tables_tab = tables_tab
        self.run_tab = run_tab
        self.scheduler_timer = None
        self.scheduler_running = False
        self.last_run_time = {}
        self._build_ui()
        self._load_schedules()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # --- Built-in Scheduler ---
        sched_group = QGroupBox("Eingebauter Scheduler (läuft, während GUI offen ist)")
        sched_layout = QVBoxLayout()

        # Schedule table
        self.sched_table = QTableWidget()
        self.sched_table.setColumnCount(5)
        self.sched_table.setHorizontalHeaderLabels([
            "Tabelle", "Intervall", "Modus", "Window", "Aktiv"
        ])
        self.sched_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.sched_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.sched_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.sched_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.sched_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        sched_layout.addWidget(self.sched_table)

        # Scheduler controls
        sched_btn_layout = QHBoxLayout()
        self.btn_add_sched = QPushButton("+ Job hinzufügen")
        self.btn_add_sched.clicked.connect(self._add_schedule)
        self.btn_del_sched = QPushButton("- Job entfernen")
        self.btn_del_sched.clicked.connect(self._del_schedule)
        self.btn_save_sched = QPushButton("Speichern")
        self.btn_save_sched.clicked.connect(self._save_schedules)
        self.btn_start_sched = QPushButton("Scheduler starten")
        self.btn_start_sched.clicked.connect(self._start_scheduler)
        self.btn_start_sched.setStyleSheet("font-weight: bold;")
        self.btn_stop_sched = QPushButton("Scheduler stoppen")
        self.btn_stop_sched.clicked.connect(self._stop_scheduler)
        self.btn_stop_sched.setEnabled(False)
        self.btn_stop_sched.setStyleSheet("color: #CC0000; font-weight: bold;")

        sched_btn_layout.addWidget(self.btn_add_sched)
        sched_btn_layout.addWidget(self.btn_del_sched)
        sched_btn_layout.addStretch()
        sched_btn_layout.addWidget(self.btn_save_sched)
        sched_btn_layout.addStretch()
        sched_btn_layout.addWidget(self.btn_start_sched)
        sched_btn_layout.addWidget(self.btn_stop_sched)
        sched_layout.addLayout(sched_btn_layout)

        # Status label
        self.sched_status = QLabel("Scheduler: gestoppt")
        self.sched_status.setStyleSheet("color: #888; font-style: italic;")
        sched_layout.addWidget(self.sched_status)

        sched_group.setLayout(sched_layout)
        layout.addWidget(sched_group)

        # --- Windows Task Scheduler Export ---
        win_group = QGroupBox("Windows-Aufgabe erstellen (läuft auch wenn GUI geschlossen ist)")
        win_layout = QVBoxLayout()

        win_form = QFormLayout()

        self.task_name = QLineEdit("SAP_Replication_Daily")
        win_form.addRow("Aufgabenname:", self.task_name)

        self.task_time = QLineEdit("02:00")
        self.task_time.setMaximumWidth(80)
        self.task_time.setPlaceholderText("HH:MM")
        win_form.addRow("Startzeit:", self.task_time)

        self.task_interval = QComboBox()
        self.task_interval.addItems(["daily", "weekly", "hourly", "every2h", "every4h", "every6h"])
        self.task_interval.currentTextChanged.connect(self._on_task_interval_changed)
        win_form.addRow("Intervall:", self.task_interval)

        self.task_mode = QComboBox()
        self.task_mode.addItems(["sync", "sync_schema", "init_only"])
        win_form.addRow("Aktion:", self.task_mode)

        # Service account options
        self.task_user = QLineEdit()
        self.task_user.setPlaceholderText("SYSTEM oder DOMÄNE\\service_user (leer = aktueller User)")
        win_form.addRow("Ausführen als:", self.task_user)

        self.task_password = QLineEdit()
        self.task_password.setEchoMode(QLineEdit.Password)
        self.task_password.setPlaceholderText("Passwort (leer bei SYSTEM)")
        win_form.addRow("Passwort:", self.task_password)

        self.task_highest = QCheckBox("Mit höchsten Privilegien")
        self.task_highest.setChecked(True)
        win_form.addRow("", self.task_highest)

        self.task_run_background = QCheckBox("Auch ausführen, wenn Benutzer nicht angemeldet ist")
        self.task_run_background.setChecked(True)
        win_form.addRow("", self.task_run_background)

        win_layout.addLayout(win_form)

        # Export button
        self.btn_export_task = QPushButton("Windows-Aufgabe erstellen (als .bat + .xml Export)")
        self.btn_export_task.clicked.connect(self._export_windows_task)
        self.btn_export_task.setStyleSheet("font-weight: bold;")
        win_layout.addWidget(self.btn_export_task)

        # Info text
        info = QLabel(
            "Erstellt eine .bat-Datei und eine Windows-Aufgabe, die den CLI-Client\n"
            "regelmäßig aufruft. Funktioniert unabhängig vom GUI-Client.\n"
            "Die Aufgabe wird im Windows Task Scheduler unter dem angegebenen Namen angelegt."
        )
        info.setStyleSheet("color: #666; font-size: 11px;")
        win_layout.addWidget(info)

        win_group.setLayout(win_layout)
        layout.addWidget(win_group)

        layout.addStretch()

    def _on_task_interval_changed(self, interval: str):
        if interval == "daily":
            self.task_time.setEnabled(True)
            self.task_time.setText("02:00")
        elif interval == "weekly":
            self.task_time.setEnabled(True)
            self.task_time.setText("02:00")
        else:
            # Hourly intervals — time is irrelevant
            self.task_time.setEnabled(False)
            self.task_time.setText("00:00")

    def _load_schedules(self):
        config = self.config_manager.get()
        schedules = config.get('schedules', [])
        self.sched_table.setRowCount(len(schedules))
        for i, s in enumerate(schedules):
            self._set_sched_row(i, s)

    def _set_sched_row(self, row: int, s: dict):
        self.sched_table.setItem(row, 0, QTableWidgetItem(s.get('table', '')))

        interval_combo = QComboBox()
        interval_combo.addItems(SCHEDULE_INTERVALS)
        interval_combo.setCurrentText(s.get('interval', 'daily'))
        self.sched_table.setCellWidget(row, 1, interval_combo)

        mode_combo = QComboBox()
        mode_combo.addItems(['sync', 'sync_schema', 'init_only'])
        mode_combo.setCurrentText(s.get('action', 'sync'))
        self.sched_table.setCellWidget(row, 2, mode_combo)

        window_combo = QComboBox()
        window_combo.addItems([''] + WINDOW_OPTIONS)
        window_combo.setCurrentText(s.get('window', 'day'))
        self.sched_table.setCellWidget(row, 3, window_combo)

        active_check = QCheckBox()
        active_check.setChecked(s.get('active', True))
        self.sched_table.setCellWidget(row, 4, active_check)

    def _add_schedule(self):
        row = self.sched_table.rowCount()
        self.sched_table.insertRow(row)
        self._set_sched_row(row, {
            'table': 'MARA',
            'interval': 'daily',
            'action': 'sync',
            'window': 'day',
            'active': True
        })

    def _del_schedule(self):
        row = self.sched_table.currentRow()
        if row >= 0:
            self.sched_table.removeRow(row)

    def _read_sched_row(self, row: int) -> dict:
        def get_text(col):
            item = self.sched_table.item(row, col)
            return item.text() if item else ''

        def get_combo(col):
            w = self.sched_table.cellWidget(row, col)
            return w.currentText() if isinstance(w, QComboBox) else ''

        def get_check(col):
            w = self.sched_table.cellWidget(row, col)
            return w.isChecked() if isinstance(w, QCheckBox) else True

        return {
            'table': get_text(0),
            'interval': get_combo(1),
            'action': get_combo(2),
            'window': get_combo(3),
            'active': get_check(4)
        }

    def _save_schedules(self):
        ok, msg = self._save_schedules_silent()
        if ok:
            QMessageBox.information(self, "Gespeichert", msg)
        else:
            QMessageBox.critical(self, "Fehler", msg)

    def _save_schedules_silent(self):
        """Save schedules without showing a dialog. Returns (success, message)."""
        schedules = []
        for i in range(self.sched_table.rowCount()):
            s = self._read_sched_row(i)
            if s['table']:
                schedules.append(s)

        config = self.config_manager.get()
        config['schedules'] = schedules
        try:
            self.config_manager.set(config)
            return True, f"{len(schedules)} Jobs gespeichert."
        except Exception as e:
            return False, f"Speichern fehlgeschlagen: {e}"

    def _interval_to_seconds(self, interval: str) -> int:
        return {
            'hourly': 3600,
            'every2h': 7200,
            'every4h': 14400,
            'every6h': 21600,
            'daily': 86400,
            'weekly': 604800,
        }.get(interval, 86400)

    def _start_scheduler(self):
        # Save first (silently — no dialog during scheduler start)
        ok, msg = self._save_schedules_silent()
        if not ok:
            QMessageBox.critical(self, "Fehler", msg)
            return

        config = self.config_manager.get()
        schedules = config.get('schedules', [])
        active = [s for s in schedules if s.get('active', True)]
        if not active:
            QMessageBox.warning(self, "Keine Jobs", "Keine aktiven Jobs im Scheduler.")
            return

        self.scheduler_running = True
        now = time.time()
        self.last_run_time = {s['table']: now for s in active if s.get('table')}

        # Timer for checking schedules every 60 seconds
        self.scheduler_timer = QTimer()
        self.scheduler_timer.timeout.connect(self._check_schedules)
        self.scheduler_timer.start(60000)  # check every minute

        self.btn_start_sched.setEnabled(False)
        self.btn_stop_sched.setEnabled(True)
        self.sched_status.setText(f"Scheduler: läuft ({len(active)} aktive Jobs)")
        self.sched_status.setStyleSheet("color: #008800; font-weight: bold;")

        # Run immediately for first check
        self._check_schedules()

    def _stop_scheduler(self):
        if self.scheduler_timer:
            self.scheduler_timer.stop()
            self.scheduler_timer = None

        self.scheduler_running = False
        self.btn_start_sched.setEnabled(True)
        self.btn_stop_sched.setEnabled(False)
        self.sched_status.setText("Scheduler: gestoppt")
        self.sched_status.setStyleSheet("color: #888; font-style: italic;")

    def _check_schedules(self):
        if not self.scheduler_running:
            return

        if self.run_tab.worker and self.run_tab.worker.isRunning():
            # A sync is already running — skip
            return

        now = time.time()
        config = self.config_manager.get()
        schedules = config.get('schedules', [])

        for s in schedules:
            if not s.get('active', True):
                continue

            table = s.get('table', '')
            interval = s.get('interval', 'daily')
            action = s.get('action', 'sync')
            window = s.get('window', 'day')

            interval_sec = self._interval_to_seconds(interval)
            last_run = self.last_run_time.get(table, 0)

            if (now - last_run) >= interval_sec:
                # Find table config
                table_cfg = None
                for t in config.get('tables', []):
                    if t.get('name', '') == table:
                        table_cfg = t
                        break

                if table_cfg:
                    self.run_tab._log("INFO",
                        f"Scheduler: starte {action} für {table} (Intervall: {interval})")

                    if action == 'sync':
                        # Override window from schedule config
                        if window:
                            table_cfg = dict(table_cfg)
                            table_cfg['window'] = window
                        self.run_tab._scheduler_triggered = True
                        self.run_tab._start_worker([table_cfg], "sync")
                    elif action == 'sync_schema':
                        self.run_tab._scheduler_triggered = True
                        self.run_tab._start_worker([table_cfg], "sync_schema")
                    elif action == 'init_only':
                        self.run_tab._scheduler_triggered = True
                        self.run_tab._start_worker([table_cfg], "init_only")

                    self.last_run_time[table] = now
                    break  # Only one job at a time
                else:
                    self.run_tab._log("WARNING",
                        f"Scheduler: Tabelle '{table}' nicht in Konfiguration gefunden — Job übersprungen")
                    self.last_run_time[table] = now

    def _export_windows_task(self):

        task_name = self.task_name.text().strip()
        if not task_name:
            QMessageBox.warning(self, "Fehler", "Aufgabenname darf nicht leer sein.")
            return

        interval = self.task_interval.currentText()
        mode = self.task_mode.currentText()
        start_time = self.task_time.text().strip()

        # Validate time format for daily/weekly
        if interval in ('daily', 'weekly'):
            try:
                datetime.strptime(start_time, '%H:%M')
            except ValueError:
                QMessageBox.warning(self, "Fehler",
                    f"Ungültige Zeitangabe: '{start_time}'\nBitte Format HH:MM verwenden (z.B. 02:00).")
                return
        run_user = self.task_user.text().strip().upper()
        run_password = self.task_password.text()
        highest_priv = self.task_highest.isChecked()
        run_background = self.task_run_background.isChecked()

        # Get paths — use sys.executable dir when frozen (PyInstaller), __file__ dir otherwise
        if getattr(sys, 'frozen', False):
            app_dir = os.path.dirname(sys.executable)
        else:
            app_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(app_dir, "config.json")
        python_exe = sys.executable
        script_path = os.path.join(app_dir, "sap_replicate.py")
        log_dir = os.path.join(app_dir, "logs")

        # Build CLI command — always log to file
        cli_args = f'--config "{config_path}"'
        if mode == 'sync_schema':
            cli_args += ' --sync-schema-all'
        elif mode == 'init_only':
            cli_args += ' --init-only'

        # Build .bat file with logging
        bat_content = f"""@echo off
REM SAP Data Replication — Auto-generated task: {task_name}
REM Interval: {interval}
REM Action: {mode}
REM Created: {datetime.now().isoformat()}
REM Runs without GUI — uses CLI client with file logging

cd /d "{app_dir}"

REM Create log directory if not exists
if not exist "{log_dir}" mkdir "{log_dir}"

REM Run replication with file logging (sap_replicate.py logs to logs/ automatically)
"{python_exe}" "{script_path}" {cli_args}

REM Exit code is propagated to Task Scheduler
exit /b %ERRORLEVEL%
"""

        bat_path = os.path.join(app_dir, f"{task_name}.bat")
        try:
            with open(bat_path, 'w', encoding='utf-8') as f:
                f.write(bat_content)
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f".bat-Datei konnte nicht erstellt werden:\n{e}")
            return

        # Build schtasks command
        schedule_type = {
            'daily': 'DAILY',
            'weekly': 'WEEKLY',
            'hourly': 'HOURLY',
            'every2h': 'MINUTE',
            'every4h': 'MINUTE',
            'every6h': 'MINUTE',
        }.get(interval, 'DAILY')

        schtasks_cmd = [
            'schtasks', '/Create', '/TN', task_name,
            '/TR', f'"{bat_path}"',
            '/SC', schedule_type,
            '/F'  # force overwrite
        ]

        # Add modifier for minute-based intervals
        if schedule_type == 'MINUTE':
            minutes = {'every2h': 120, 'every4h': 240, 'every6h': 360}.get(interval, 120)
            schtasks_cmd.extend(['/MO', str(minutes)])

        # Add start time for daily/weekly
        if schedule_type in ('DAILY', 'WEEKLY'):
            schtasks_cmd.extend(['/ST', start_time])

        # Run as user
        if run_user:
            schtasks_cmd.extend(['/RU', run_user])
            if run_password and run_user != 'SYSTEM':
                schtasks_cmd.extend(['/RP', run_password])
        elif run_background:
            # Run as SYSTEM when background is requested but no user specified
            schtasks_cmd.extend(['/RU', 'SYSTEM'])

        # Highest privileges
        if highest_priv:
            schtasks_cmd.extend(['/RL', 'HIGHEST'])

        try:
            result = subprocess.run(schtasks_cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                msg = f"Windows-Aufgabe '{task_name}' wurde erstellt.\n\n"
                msg += f".bat-Datei: {bat_path}\n"
                msg += f"Log-Datei:  {log_dir}\\sap_replicate_<datum>.log\n\n"
                msg += f"Ausführen als: {run_user or 'SYSTEM'}\n"
                msg += f"Intervall: {interval}\n"
                if start_time and schedule_type in ('DAILY', 'WEEKLY'):
                    msg += f"Startzeit: {start_time}\n"
                msg += f"\nDie Aufgabe läuft automatisch im Windows Task Scheduler.\n"
                msg += f"Der GUI-Client muss dafür nicht geöffnet sein.\n"
                msg += f"Logs werden täglich in {log_dir} geschrieben."
                QMessageBox.information(self, "Erfolg", msg)
            else:
                QMessageBox.warning(self, "Hinweis",
                    f"Windows-Aufgabe konnte nicht erstellt werden.\n\n"
                    f"Mögliche Ursachen:\n"
                    f"- Keine Admin-Rechte (als Administrator ausführen)\n"
                    f"- Falscher Service-Account oder Passwort\n\n"
                    f".bat-Datei wurde erstellt: {bat_path}\n\n"
                    f"schtasks-Befehl:\n{' '.join(schtasks_cmd)}\n\n"
                    f"Fehler: {result.stderr}")
        except Exception as e:
            QMessageBox.critical(self, "Fehler",
                f"Windows-Aufgabe konnte nicht erstellt werden:\n{e}\n\n"
                f".bat-Datei wurde erstellt: {bat_path}\n"
                f"Log-Verzeichnis: {log_dir}")


# ============================================================================
# Main Window
# ============================================================================

class MainWindow(QMainWindow):
    """Main application window."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("SAP Data Replication")
        self.setMinimumSize(QSize(900, 700))
        self.resize(1100, 800)

        self.config_manager = ConfigManager()
        self.config_manager.load()

        self._build_ui()
        self._build_menu()

    def _build_ui(self):
        central = QWidget()
        layout = QVBoxLayout(central)

        # Tab Widget
        self.tabs = QTabWidget()

        self.settings_tab = SettingsTab(self.config_manager)
        self.tables_tab = TablesTab(self.config_manager)
        self.run_tab = RunTab(self.config_manager, self.tables_tab)
        self.schedule_tab = ScheduleTab(self.config_manager, self.tables_tab, self.run_tab)

        self.tabs.addTab(self.settings_tab, "Verbindungen")
        self.tabs.addTab(self.tables_tab, "Tabellen")
        self.tabs.addTab(self.run_tab, "Ausführen")
        self.tabs.addTab(self.schedule_tab, "Zeitplan")

        # Context-sensitive help on tab change
        self.tabs.currentChanged.connect(self._on_tab_changed)

        layout.addWidget(self.tabs)

        # Status Bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Bereit")
        self.run_tab.status_bar = self.status_bar

        central.setLayout(layout)
        self.setCentralWidget(central)

    def _build_menu(self):
        menubar = self.menuBar()

        # File Menu
        file_menu = menubar.addMenu("Datei")

        action_save = QAction("Konfiguration speichern", self)
        action_save.triggered.connect(self._save_all)
        file_menu.addAction(action_save)

        action_reload = QAction("Konfiguration neu laden", self)
        action_reload.triggered.connect(self._reload_config)
        file_menu.addAction(action_reload)

        file_menu.addSeparator()

        action_exit = QAction("Beenden", self)
        action_exit.triggered.connect(self.close)
        file_menu.addAction(action_exit)

        # Help Menu
        help_menu = menubar.addMenu("Hilfe")

        action_help_settings = QAction("Hilfe: Verbindungen", self)
        action_help_settings.triggered.connect(lambda: self._show_help("settings"))
        help_menu.addAction(action_help_settings)

        action_help_tables = QAction("Hilfe: Tabellen", self)
        action_help_tables.triggered.connect(lambda: self._show_help("tables"))
        help_menu.addAction(action_help_tables)

        action_help_run = QAction("Hilfe: Ausführen", self)
        action_help_run.triggered.connect(lambda: self._show_help("run"))
        help_menu.addAction(action_help_run)

        action_help_schedule = QAction("Hilfe: Zeitplan", self)
        action_help_schedule.triggered.connect(lambda: self._show_help("schedule"))
        help_menu.addAction(action_help_schedule)

        help_menu.addSeparator()

        action_help_quickstart = QAction("Schnellstart-Anleitung", self)
        action_help_quickstart.triggered.connect(lambda: self._show_help("quickstart"))
        help_menu.addAction(action_help_quickstart)

        help_menu.addSeparator()

        action_about = QAction("Über", self)
        action_about.triggered.connect(self._show_about)
        help_menu.addAction(action_about)

    def _save_all(self):
        try:
            ok1, msg1 = self.settings_tab._save_silent()
            ok2, msg2 = self.tables_tab._save_silent()
            ok3, msg3 = self.schedule_tab._save_schedules_silent()
            if ok1 and ok2 and ok3:
                self.settings_tab.config_changed.emit()
                self.tables_tab.config_changed.emit()
                self.status_bar.showMessage("Konfiguration gespeichert", 3000)
                QMessageBox.information(self, "Gespeichert", "Alle Einstellungen erfolgreich gespeichert.")
            else:
                errors = [m for ok, m in [(ok1, msg1), (ok2, msg2), (ok3, msg3)] if not ok]
                QMessageBox.critical(self, "Fehler", "\n".join(errors))
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Speichern fehlgeschlagen: {e}")

    def _reload_config(self):
        self.config_manager.load()
        self.settings_tab.config = self.config_manager.get()
        self.settings_tab._load_values()
        self.tables_tab.config = self.config_manager.get()
        self.tables_tab._load_tables()
        self.schedule_tab.config = self.config_manager.get()
        self.schedule_tab._load_schedules()
        self.status_bar.showMessage("Konfiguration neu geladen", 3000)

    def _on_tab_changed(self, index: int):
        """Update status bar with hint when tab changes."""
        hints = {
            0: "Verbindungen — SAP, SQL Server, SSH und Flatfile-Übertragung konfigurieren",
            1: "Tabellen — Tabellen und Replikationsmodi konfigurieren",
            2: "Ausführen — Sync starten, Schema erstellen, CDC verwalten",
            3: "Zeitplan — Automatische Jobs und Windows-Aufgaben einrichten",
        }
        hint = hints.get(index, "Bereit")
        self.status_bar.showMessage(hint)
        # Also show in the schedule tab's status if available
        if index == 3 and hasattr(self, 'schedule_tab'):
            if not self.schedule_tab.scheduler_running:
                self.schedule_tab.sched_status.setText(
                    "Tipp: Hilfe → Hilfe: Zeitplan für Anleitung"
                )

    def _show_about(self):
        QMessageBox.about(self, "Über SAP Data Replication",
            "<h3>SAP Data Replication Client</h3>"
            "<p>Replikation von SAP-Tabellen in Microsoft SQL Server</p>"
            "<p><b>Modi:</b> CDC (Trigger), Timeframe, Full-Load, Flatfile</p>"
            "<p><b>Lizenz:</b> GPL-3.0</p>"
            "<p><b>GitHub:</b> https://github.com/vbkredeemer/sap-data-replication</p>")

    def _show_help(self, topic: str):
        """Show context-sensitive help dialog for the given topic."""
        helps = {
            "quickstart": (
                "Schnellstart-Anleitung",
                self._help_quickstart()
            ),
            "settings": (
                "Hilfe: Verbindungen",
                self._help_settings()
            ),
            "tables": (
                "Hilfe: Tabellen",
                self._help_tables()
            ),
            "run": (
                "Hilfe: Ausführen",
                self._help_run()
            ),
            "schedule": (
                "Hilfe: Zeitplan",
                self._help_schedule()
            ),
        }

        if topic in helps:
            title, content = helps[topic]
            self._show_help_dialog(title, content)

    def _show_help_dialog(self, title: str, content: str):
        """Show a scrollable help dialog with formatted HTML content."""
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QPushButton, QScrollArea

        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setMinimumSize(QSize(700, 500))
        dialog.resize(800, 600)

        layout = QVBoxLayout(dialog)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)

        label = QLabel(content)
        label.setWordWrap(True)
        label.setTextFormat(Qt.RichText)
        label.setStyleSheet("font-size: 13px; padding: 10px;")
        scroll.setWidget(label)

        layout.addWidget(scroll)

        btn_close = QPushButton("Schließen")
        btn_close.clicked.connect(dialog.accept)
        layout.addWidget(btn_close)

        dialog.exec()

    def _help_quickstart(self):
        return """<h2>Schnellstart-Anleitung</h2>

<h3>Schritt 1: SAP-Funktionsbausteine installieren</h3>
<p>Installieren Sie die folgenden Bausteine in SAP (SE37, Funktionsgruppe Z_SQL):</p>
<ul>
  <li><b>Z_READ_TABLE</b> — Chunked Table Read</li>
  <li><b>Z_EXECUTE_SQL</b> — SQL-Abfragen über ADBC</li>
  <li><b>Z_CDC_INIT</b> — CDC initialisieren (Trigger + Log-Tabelle)</li>
  <li><b>Z_CDC_READ</b> — CDC Delta abholen</li>
  <li><b>Z_CDC_CLEANUP</b> — CDC Log aufräumen / entfernen</li>
  <li><b>Z_EXPORT_TABLE</b> — Flatfile-Export (CSV)</li>
  <li><b>Z_DELETE_FILE</b> — Datei auf SAP-Server löschen</li>
</ul>
<p>Alle Bausteine verwenden vorhandene DDIC-Typen (ZSQL_FIELD, ZSQL_ROW) — keine neuen Domänen/Elemente nötig.</p>

<h3>Schritt 2: Verbindungen konfigurieren</h3>
<ol>
  <li>Tab <b>Verbindungen</b> öffnen</li>
  <li>SAP-Daten eingeben (Host, Systemnummer, Mandant, User, Passwort)</li>
  <li>SQL Server Connection String eingeben</li>
  <li><b>Speichern</b> klicken</li>
  <li><b>SAP testen</b> und <b>SQL Server testen</b> klicken</li>
</ol>

<h3>Schritt 3: Tabellen konfigurieren</h3>
<ol>
  <li>Tab <b>Tabellen</b> öffnen</li>
  <li><b>+ Tabelle hinzufügen</b> klicken</li>
  <li>Tabellennamen (z.B. MARA), Modus (z.B. timeframe), Delta-Feld (z.B. AEDAT), Window (z.B. day) eintragen</li>
  <li><b>Speichern</b> klicken</li>
</ol>

<h3>Schritt 4: Schema erstellen</h3>
<ol>
  <li>Tab <b>Ausführen</b> öffnen</li>
  <li><b>Alle Schemata erstellen</b> klicken — erstellt Tabellen + Indizes in MSSQL</li>
</ol>

<h3>Schritt 5: Erst-Load</h3>
<ol>
  <li>Tab <b>Ausführen</b> → <b>Alle aktiven Tabellen syncen</b> klicken</li>
  <li>Warten bis Sync abgeschlossen ist</li>
</ol>

<h3>Schritt 6: Zeitplan einrichten</h3>
<ol>
  <li>Tab <b>Zeitplan</b> öffnen</li>
  <li>Jobs hinzufügen (Tabelle + Intervall)</li>
  <li>Entweder <b>Scheduler starten</b> (GUI muss offen bleiben) oder <b>Windows-Aufgabe erstellen</b> (läuft automatisch)</li>
</ol>

<h3>Empfohlene Modi pro Tabellentyp</h3>
<table border="1" cellpadding="4" cellspacing="0">
<tr><th>Tabellentyp</th><th>Modus</th><th>Window</th><th>Grund</th></tr>
<tr><td>Stammdaten (MARA, KNA1)</td><td>timeframe</td><td>month</td><td>Wenige Änderungen, AEDAT zuverlässig</td></tr>
<tr><td>Bewegungsdaten (VBAK, VBAP)</td><td>timeframe</td><td>day</td><td>Tägliche Änderungen, AEDAT vorhanden</td></tr>
<tr><td>Große Tabellen (ACDOCA, MSEG)</td><td>flatfile</td><td>day</td><td>3-5x schneller als RFC</td></tr>
<tr><td>Kleine Tabellen (T001W)</td><td>full</td><td>—</td><td>Trivial, einfach komplett laden</td></tr>
<tr><td>Mit Delta-Queue nötig</td><td>cdc</td><td>—</td><td>Trigger-basiert, erfasst DELETEs</td></tr>
</table>
"""

    def _help_settings(self):
        return """<h2>Verbindungen konfigurieren</h2>

<p>In diesem Tab werden alle Verbindungen eingerichtet, die der Client für die Datenreplikation benötigt.</p>

<h3>SAP-Verbindung (RFC)</h3>
<p>Verbindung zum SAP-Applikationsserver über das SAP NWRFC SDK.</p>
<table border="1" cellpadding="4" cellspacing="0">
<tr><th>Feld</th><th>Beschreibung</th><th>Beispiel</th></tr>
<tr><td><b>Application Server</b></td><td>Hostname oder IP des SAP-Applikationsservers</td><td>sap-prod.firma.de</td></tr>
<tr><td><b>Systemnummer</b></td><td>SAP-Systemnummer (zweistellig)</td><td>10</td></tr>
<tr><td><b>Mandant</b></td><td>SAP-Mandant (dreistellig)</td><td>100</td></tr>
<tr><td><b>User</b></td><td>SAP-User für RFC-Aufrufe</td><td>RFC_USER</td></tr>
<tr><td><b>Passwort</b></td><td>Passwort des SAP-Users</td><td>********</td></tr>
<tr><td><b>Sprache</b></td><td>Anmeldesprache (EN empfohlen)</td><td>EN</td></tr>
</table>
<p><b>SAP testen:</b> Stellt eine Testverbindung her und prüft ob die RFC-Parameter korrekt sind.</p>

<h3>SQL Server Verbindung</h3>
<p>Verbindung zur Zieldatenbank (Microsoft SQL Server) über ODBC.</p>
<table border="1" cellpadding="4" cellspacing="0">
<tr><th>Feld</th><th>Beschreibung</th></tr>
<tr><td><b>Connection String</b></td><td>ODBC-Verbindungsstring für den SQL Server</td></tr>
</table>
<p>Format:<br>
<code>Driver={ODBC Driver 17 for SQL Server};Server=localhost;Database=SAP_REPL;Trusted_Connection=yes;</code></p>
<p>Alternativ mit SQL-Authentifizierung:<br>
<code>Driver={ODBC Driver 17 for SQL Server};Server=myserver;Database=SAP_REPL;UID=myuser;PWD=mypassword;</code></p>
<p><b>SQL Server testen:</b> Stellt eine Testverbindung her.</p>

<h3>SSH-Verbindung (für Flatfile-Modus)</h3>
<p>Wird nur benötigt, wenn der Flatfile-Modus mit Übertragungsmethode <b>SCP</b> verwendet wird. Für SMB oder Local wird keine SSH-Verbindung benötigt.</p>
<table border="1" cellpadding="4" cellspacing="0">
<tr><th>Feld</th><th>Beschreibung</th></tr>
<tr><td><b>Host</b></td><td>SSH-Host des SAP-Servers (meist gleich wie Application Server)</td></tr>
<tr><td><b>User</b></td><td>SSH-User (z.B. sapadm)</td></tr>
<tr><td><b>Key File</b></td><td>Pfad zum privaten SSH-Schlüssel (z.B. C:\\keys\\sap_id_rsa)</td></tr>
<tr><td><b>Port</b></td><td>SSH-Port (Standard: 22)</td></tr>
</table>

<h3>Flatfile-Übertragung</h3>
<p>Definiert wie die CSV-Dateien vom SAP-Server auf den MSSQL-Server übertragen werden.</p>
<table border="1" cellpadding="4" cellspacing="0">
<tr><th>Methode</th><th>Beschreibung</th><th>Voraussetzung</th></tr>
<tr><td><b>scp</b></td><td>SSH Secure Copy — Datei-Transfer über SSH</td><td>SSH-Verbindung konfiguriert, SSH-Key</td></tr>
<tr><td><b>smb</b></td><td>Windows-Netzlaufwerk (UNC-Pfad)</td><td>SAP-Server hat Samba-Share freigegeben</td></tr>
<tr><td><b>local</b></td><td>Datei ist lokal verfügbar (NFS-Mount)</td><td>SAP-Verzeichnis ist auf MSSQL-Server gemountet</td></tr>
</table>
<p><b>SMB-Share (UNC-Pfad):</b> z.B. <code>\\\\sap-server\\sap\\tmp</code> — der SAP-Server schreibt nach /usr/sap/tmp/, der MSSQL-Server greift über das Windows-Share zu.</p>
"""

    def _help_tables(self):
        return """<h2>Tabellen konfigurieren</h2>

<p>In diesem Tab definieren Sie welche SAP-Tabellen repliziert werden sollen und mit welchem Modus.</p>

<h3>Schaltflächen</h3>
<table border="1" cellpadding="4" cellspacing="0">
<tr><th>Schaltfläche</th><th>Funktion</th></tr>
<tr><td><b>+ Tabelle hinzufügen</b></td><td>Fügt eine neue Zeile hinzu mit Default-Werten</td></tr>
<tr><td><b>- Entfernen</b></td><td>Entfernt die ausgewählte Zeile</td></tr>
<tr><td><b>Speichern</b></td><td>Speichert die Tabellen-Konfiguration in config.json</td></tr>
<tr><td><b>Rechtsklick</b></td><td>Kontextmenü: Zeile duplizieren oder entfernen</td></tr>
</table>

<h3>Felder pro Tabelle</h3>
<table border="1" cellpadding="4" cellspacing="0">
<tr><th>Feld</th><th>Beschreibung</th><th>Pflicht?</th><th>Beispiel</th></tr>
<tr><td><b>Tabelle</b></td><td>Name der SAP-Tabelle</td><td>Ja</td><td>MARA</td></tr>
<tr><td><b>Modus</b></td><td>Replikationsmodus (Dropdown)</td><td>Ja</td><td>timeframe</td></tr>
<tr><td><b>Key Fields</b></td><td>Primärschlüsselfelder (kommagetrennt). Nur für CDC-Modus nötig.</td><td>Nur CDC</td><td>MATNR</td></tr>
<tr><td><b>Delta Field</b></td><td>Feldname des Änderungsdatums. Für timeframe und flatfile.</td><td>Zeitraum-Modi</td><td>AEDAT</td></tr>
<tr><td><b>Window</b></td><td>Zeitfenster (Dropdown): day, week, month, year, all</td><td>Zeitraum-Modi</td><td>day</td></tr>
<tr><td><b>Target Table</b></td><td>Name der Zieltabelle in MSSQL. Leer = gleicher Name wie SAP.</td><td>Nein</td><td>MARA (oder leer)</td></tr>
<tr><td><b>Replace Mode</b></td><td>Wie Daten in Zieltabelle ersetzt werden (Dropdown)</td><td>Nein</td><td>replace_window</td></tr>
<tr><td><b>Chunk Size</b></td><td>Anzahl Zeilen pro RFC-Aufruf (nur RFC-basierte Modi)</td><td>Nein</td><td>10000</td></tr>
<tr><td><b>Fields</b></td><td>Feldauswahl: * für alle, oder kommagetrennte Liste</td><td>Nein</td><td>*</td></tr>
<tr><td><b>Aktiv</b></td><td>Checkbox — nur aktive Tabellen werden bei "Alle syncen" berücksichtigt</td><td>Nein</td><td>☑</td></tr>
</table>

<h3>Modi im Detail</h3>

<h4>CDC (Trigger-basiert)</h4>
<p>Automatisches Delta-Handling über Datenbank-Trigger. Erfasst INSERT, UPDATE und DELETE.</p>
<ul>
  <li><b>Key Fields:</b> Pflicht — Primärschlüsselfelder, z.B. "MATNR" oder "MANDT,MATNR"</li>
  <li><b>Voraussetzung:</b> Z_CDC_INIT muss einmalig ausgeführt werden (Tab "Ausführen" → "CDC initialisieren")</li>
  <li><b>Ablauf:</b> Trigger loggt Änderungen → Z_CDC_READ liefert Delta → Z_CDC_CLEANUP räumt auf</li>
  <li><b>Vorteil:</b> Echtes CDC inkl. DELETE-Erkennung, automatisches Delta</li>
  <li><b>Nachteil:</b> Trigger kann bei SAP-Upgrades verloren gehen (wird durch nächtlichen Check erkannt)</li>
</ul>

<h4>Timeframe (Zeitfenster-Delta)</h4>
<p>Trigger-frei — lädt geänderte Daten über das Änderungsdatum (z.B. AEDAT, LAEDA).</p>
<ul>
  <li><b>Delta Field:</b> Feldname des Änderungsdatums in der SAP-Tabelle</li>
  <li><b>Window:</b> Zeitfenster — lädt immer aktuellen + vorherigen Zeitraum</li>
  <li><b>Ablauf:</b> DELETE Zeitraum in MSSQL → Z_READ_TABLE mit WHERE → INSERT</li>
  <li><b>Vorteil:</b> Keine Trigger, upgrade-sicher, einfach</li>
  <li><b>Nachteil:</b> Keine DELETE-Erkennung für ältere Zeiträume</li>
</ul>

<h4>Full (Komplettladung)</h4>
<p>Lädt die komplette Tabelle — TRUNCATE + INSERT.</p>
<ul>
  <li><b>Für kleine Tabellen</b> (&lt; 50.000 Zeilen) empfohlen</li>
  <li>Nutzt Z_READ_TABLE mit Chunking</li>
</ul>

<h4>Flatfile (CSV-Export)</h4>
<p>Schnellster Modus für große Tabellen — SAP schreibt CSV, Python lädt via BULK INSERT.</p>
<ul>
  <li><b>3-5x schneller</b> als RFC-basierte Modi</li>
  <li><b>Delta Field + Window:</b> Optional — für Zeitraum-gefilterten Export</li>
  <li><b>Erfordert:</b> SCP/SMB/Local Übertragungsmethode konfiguriert</li>
  <li><b>Ablauf:</b> Z_EXPORT_TABLE → SCP/SMB Download → BULK INSERT → Z_DELETE_FILE</li>
</ul>

<h3>Replace Mode</h3>
<table border="1" cellpadding="4" cellspacing="0">
<tr><th>Modus</th><th>Verhalten</th><th>Wann verwenden?</th></tr>
<tr><td><b>append</b></td><td>Nur INSERT, nichts löschen</td><td>Erst-Load, Daten nur hinzufügen</td></tr>
<tr><td><b>replace_all</b></td><td>TRUNCATE Zieltabelle, dann INSERT</td><td>Full-Load, kleine Tabellen</td></tr>
<tr><td><b>replace_window</b></td><td>DELETE Zeitraum in Zieltabelle, dann INSERT</td><td>Timeframe-Delta, vermeidet Duplikate</td></tr>
</table>

<h3>Window (Zeitfenster)</h3>
<p><b>Wichtig:</b> Es wird immer der <b>aktuelle UND der vorherige Zeitraum</b> geladen und ersetzt. Diese Überlappung verhindert Datenverlust beim Periodenwechsel.</p>
<table border="1" cellpadding="4" cellspacing="0">
<tr><th>Fenster</th><th>Geladen wird</th><th>Woche gilt</th></tr>
<tr><td><b>day</b></td><td>Gestern + heute</td><td>—</td></tr>
<tr><td><b>week</b></td><td>Letzte Woche (Mo-So) + diese Woche</td><td>Montag bis Sonntag</td></tr>
<tr><td><b>month</b></td><td>Letzter Monat + aktueller Monat</td><td>—</td></tr>
<tr><td><b>year</b></td><td>Letztes Jahr + aktuelles Jahr</td><td>—</td></tr>
<tr><td><b>all</b></td><td>Komplette Tabelle (TRUNCATE + INSERT)</td><td>—</td></tr>
</table>
"""

    def _help_run(self):
        return """<h2>Ausführen</h2>

<p>In diesem Tab starten Sie Replikationsvorgänge und überwachen den Fortschritt.</p>

<h3>Aktionen</h3>
<table border="1" cellpadding="4" cellspacing="0">
<tr><th>Schaltfläche</th><th>Funktion</th></tr>
<tr><td><b>Alle aktiven Tabellen syncen</b></td><td>Führt Sync für alle aktiven Tabellen aus (Tab "Tabellen" → Aktiv ☑)</td></tr>
<tr><td><b>Ausgewählte Tabelle syncen</b></td><td>Sync nur für die im Tab "Tabellen" markierte Tabelle</td></tr>
<tr><td><b>CDC initialisieren</b></td><td>Prüft/legt Trigger + Log-Tabelle für alle CDC-Tabellen an. Idempotent — kann mehrfach ausgeführt werden.</td></tr>
<tr><td><b>CDC entfernen</b></td><td>Entfernt CDC für die ausgewählte Tabelle (Trigger + Log-Tabelle werden gelöscht). Mit Bestätigungsdialog.</td></tr>
<tr><td><b>Schema erstellen</b></td><td>Erstellt Tabelle + Indizes in MSSQL aus SAP-Metadaten (DD03L, DD12L, DD17S). DROP + CREATE TABLE.</td></tr>
<tr><td><b>Alle Schemata erstellen</b></td><td>Schema-Erstellung für alle aktiven Tabellen.</td></tr>
<tr><td><b>Abbrechen</b></td><td>Bricht einen laufenden Sync ab (nur während Sync aktiv)</td></tr>
</table>

<h3>Fortschritts-Tabelle</h3>
<p>Zeigt pro Tabelle den Status und die Uhrzeit an:</p>
<ul>
  <li><span style="color:#0066CC">Blau "Running..."</span> — Sync läuft</li>
  <li><span style="color:#008800">Grün "✓ Success"</span> — Erfolgreich abgeschlossen</li>
  <li><span style="color:#CC0000">Rot "✗ Failed" / "✗ Error"</span> — Fehler aufgetreten</li>
</ul>

<h3>Protokoll</h3>
<p>Echtzeit-Log-Ausgabe mit farblichen Level-Markierungen:</p>
<ul>
  <li><span style="color:#000000">Schwarz</span> — INFO (normale Meldungen)</li>
  <li><span style="color:#CC8800">Orange</span> — WARNING (Warnungen)</li>
  <li><span style="color:#CC0000">Rot</span> — ERROR (Fehler)</li>
</ul>
<p><b>Protokoll löschen:</b> Leert die Log-Ausgabe (nur Anzeige, Log-Datei wird nicht gelöscht).</p>
<p>Sync läuft in einem Hintergrund-Thread — die GUI bleibt bedienbar während des Syncs.</p>

<h3>Empfohlene Reihenfolge</h3>
<ol>
  <li><b>Schema erstellen</b> (einmalig oder nach SAP-Änderungen) — erstellt Tabellen + Indizes</li>
  <li><b>Sync</b> — lädt Daten (erst-Load oder Delta)</li>
  <li><b>CDC initialisieren</b> (nur für CDC-Tabellen) — aktiviert Trigger</li>
</ol>
"""

    def _help_schedule(self):
        return """<h2>Zeitplan</h2>

<p>In diesem Tab richten Sie automatische regelmäßige Ausführungen ein — entweder über den eingebauten Scheduler (GUI muss offen sein) oder als Windows-Aufgabe (läuft automatisch).</p>

<h3>Eingebauter Scheduler</h3>
<p>Läuft als Timer im GUI-Client. Prüft jede Minute ob ein Job fällig ist. Die GUI muss dafür offen bleiben.</p>

<h4>Job-Liste</h4>
<table border="1" cellpadding="4" cellspacing="0">
<tr><th>Feld</th><th>Beschreibung</th><th>Beispiel</th></tr>
<tr><td><b>Tabelle</b></td><td>Name der SAP-Tabelle (muss im Tab "Tabellen" konfiguriert sein)</td><td>MARA</td></tr>
<tr><td><b>Intervall</b></td><td>Wie oft soll der Job laufen? (Dropdown)</td><td>daily</td></tr>
<tr><td><b>Modus</b></td><td>Was soll ausgeführt werden? sync / sync_schema / init_only</td><td>sync</td></tr>
<tr><td><b>Window</b></td><td>Zeitfenster für sync (day, week, month, year, all)</td><td>day</td></tr>
<tr><td><b>Aktiv</b></td><td>Checkbox — nur aktive Jobs werden ausgeführt</td><td>☑</td></tr>
</table>

<h4>Verfügbare Intervalle</h4>
<table border="1" cellpadding="4" cellspacing="0">
<tr><th>Intervall</th><th>Alle...</th><th>Typische Verwendung</th></tr>
<tr><td>hourly</td><td>1 Stunde</td><td>Häufige Deltas bei Bewegungsdaten</td></tr>
<tr><td>every2h</td><td>2 Stunden</td><td>—</td></tr>
<tr><td>every4h</td><td>4 Stunden</td><td>Mittelfrequente Synchronisation</td></tr>
<tr><td>every6h</td><td>6 Stunden</td><td>4x täglich</td></tr>
<tr><td>daily</td><td>24 Stunden</td><td>Standard für nächtliche Synchronisation</td></tr>
<tr><td>weekly</td><td>7 Tage</td><td>Wöchentlicher Full-Load</td></tr>
</table>

<h4>Schaltflächen</h4>
<table border="1" cellpadding="4" cellspacing="0">
<tr><th>Schaltfläche</th><th>Funktion</th></tr>
<tr><td><b>+ Job hinzufügen</b></td><td>Neue Job-Zeile</td></tr>
<tr><td><b>- Job entfernen</b></td><td>Ausgewählte Job-Zeile löschen</td></tr>
<tr><td><b>Speichern</b></td><td>Jobs in config.json speichern</td></tr>
<tr><td><b>Scheduler starten</b></td><td>Timer aktivieren — prüft jede Minute auf fällige Jobs</td></tr>
<tr><td><b>Scheduler stoppen</b></td><td>Timer deaktivieren</td></tr>
</table>

<h3>Windows-Aufgabe erstellen</h3>
<p>Erstellt eine Windows-Aufgabe im Task Scheduler, die den CLI-Client regelmäßig aufruft. <b>Läuft auch wenn der GUI-Client geschlossen ist.</b> Keine Windows-Sitzung nötig.</p>

<table border="1" cellpadding="4" cellspacing="0">
<tr><th>Feld</th><th>Beschreibung</th><th>Beispiel</th></tr>
<tr><td><b>Aufgabenname</b></td><td>Name der Windows-Aufgabe (im Task Scheduler)</td><td>SAP_Replication_Daily</td></tr>
<tr><td><b>Startzeit</b></td><td>Uhrzeit für daily/weekly (Format HH:MM)</td><td>02:00</td></tr>
<tr><td><b>Intervall</b></td><td>daily / weekly / hourly / every2h / every4h / every6h</td><td>daily</td></tr>
<tr><td><b>Aktion</b></td><td>sync / sync_schema / init_only</td><td>sync</td></tr>
<tr><td><b>Ausführen als</b></td><td>Windows-Account für die Aufgabe. SYSTEM = läuft ohne Anmeldung. Alternativ: DOMÄNE\\service_user</td><td>SYSTEM</td></tr>
<tr><td><b>Passwort</b></td><td>Passwort für Service-Account (leer bei SYSTEM)</td><td>********</td></tr>
<tr><td><b>Mit höchsten Privilegien</b></td><td>/RL HIGHEST — empfohlen für Datenbank-Zugriff</td><td>☑</td></tr>
<tr><td><b>Auch ausführen, wenn nicht angemeldet</b></td><td>Aufgabe läuft ohne Windows-Sitzung</td><td>☑</td></tr>
</table>

<h4>Was passiert beim Klick auf "Windows-Aufgabe erstellen"</h4>
<ol>
  <li>Eine <b>.bat-Datei</b> wird generiert (ruft sap_replicate.py auf)</li>
  <li>Der <b>schtasks-Befehl</b> wird ausgeführt → erstellt die Aufgabe im Windows Task Scheduler</li>
  <li>Logs werden automatisch in <code>logs/sap_replicate_YYYYMMDD.log</code> geschrieben</li>
  <li>Der Exit-Code wird an Windows Task Scheduler weitergegeben</li>
</ol>
<p><b>Hinweis:</b> Die Erstellung der Windows-Aufgabe erfordert evtl. Administrator-Rechte. Falls dies fehlschlägt, wird die .bat-Datei trotzdem erstellt und der schtasks-Befehl angezeigt — Sie können ihn dann manuell in einer Admin-Eingabeaufforderung ausführen.</p>

<h4>Empfohlene Kombination</h4>
<table border="1" cellpadding="4" cellspacing="0">
<tr><th>Szenario</th><th>Ansatz</th></tr>
<tr><td>Nächtlicher Sync um 2:00 Uhr</td><td>Windows-Aufgabe, daily, 02:00, SYSTEM</td></tr>
<tr><td>Stündliche Deltas für Bewegungsdaten</td><td>Eingebauter Scheduler (GUI offen) oder Windows-Aufgabe hourly</td></tr>
<tr><td>Wöchentlicher Full-Load am Wochenende</td><td>Windows-Aufgabe, weekly, 01:00, window=all</td></tr>
<tr><td>CDC Trigger-Check nach Upgrade</td><td>Windows-Aufgabe, daily, action=init_only</td></tr>
</table>
"""

    def closeEvent(self, event):
        if hasattr(self, 'run_tab') and self.run_tab.worker and self.run_tab.worker.isRunning():
            reply = QMessageBox.question(self, "Vorgang läuft",
                "Ein Sync-Vorgang läuft noch. Trotzdem beenden?")
            if reply != QMessageBox.Yes:
                event.ignore()
                return
            self.run_tab.worker.cancel()
            self.run_tab.worker.wait(5000)
            if self.run_tab.worker.isRunning():
                self.run_tab.worker.terminate()
        # Stop scheduler if running
        if hasattr(self, 'schedule_tab'):
            self.schedule_tab._stop_scheduler()
        event.accept()


# ============================================================================
# Entry Point
# ============================================================================

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("SAP Data Replication")
    app.setOrganizationName("SAP-Tools")

    # Dark-ish professional style
    app.setStyle("Fusion")

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == '__main__':
    main()