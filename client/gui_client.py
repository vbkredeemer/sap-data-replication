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

import sys
import json
import os
import logging
import threading
from datetime import datetime, date
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, QThread, Signal, QObject, QSize
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QLabel, QLineEdit, QComboBox, QPushButton, QTableWidget,
    QTableWidgetItem, QHeaderView, QGroupBox, QFormLayout, QCheckBox,
    QSpinBox, QMessageBox, QFileDialog, QTextEdit, QProgressBar,
    QStatusBar, QSplitter, QMenu, QAbstractItemView
)
from PySide6.QtGui import QFont, QColor, QAction, QIcon


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
    "tables": []
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
        self.config = dict(DEFAULT_CONFIG)

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
                self.config = dict(DEFAULT_CONFIG)
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
        # Setup logging to emit to GUI
        handler = GuiLogHandler()
        handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', '%H:%M:%S'))
        handler.log_signal = self.log_message
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.INFO)

        success = 0
        fail = 0

        try:
            # Import replication logic
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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
                        if t.get('mode') == 'cdc' and t.get('key_fields'):
                            cdc = CdcReplicator(sap, sql, state)
                            cdc.init_table(t['name'], t['key_fields'])
                            self.progress.emit(t['name'], "Init OK")
                    return

                if self.action == "remove_cdc":
                    for t in self.tables_to_sync:
                        if self._cancel:
                            break
                        cdc = CdcReplicator(sap, sql, state)
                        cdc.remove_cdc(t['name'])
                        self.progress.emit(t['name'], "Removed")
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
                sap.close()
                sql.close()

        except Exception as e:
            logging.error(f"Fatal error: {e}")
            fail += 1

        finally:
            root_logger.removeHandler(handler)

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
        self.ssh_port.setValue(ssh.get('port', 22))

        ff = self.config.get('flatfile', {})
        self.ff_method.setCurrentText(ff.get('transfer_method', 'scp'))
        self.ff_smb_share.setText(ff.get('smb_share', ''))
        self._on_method_changed(self.ff_method.currentText())

    def _save(self):
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
            QMessageBox.information(self, "Gespeichert", "Konfiguration erfolgreich gespeichert.")
            self.config_changed.emit()
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Speichern fehlgeschlagen: {e}")

    def _test_sap(self):
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from pyrfc import Connection
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

class TablesTab(QWidget):
    """Table configuration: list of tables with per-table mode settings."""

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
        self.btn_save = QPushButton("Speichern")
        self.btn_save.clicked.connect(self._save)
        toolbar.addWidget(self.btn_add)
        toolbar.addWidget(self.btn_remove)
        toolbar.addStretch()
        toolbar.addWidget(self.btn_save)
        layout.addLayout(toolbar)

        # --- Table List ---
        self.table = QTableWidget()
        self.table.setColumnCount(10)
        self.table.setHorizontalHeaderLabels([
            "Tabelle", "Modus", "Key Fields", "Delta Field",
            "Window", "Target Table", "Replace Mode",
            "Chunk Size", "Fields", "Aktiv"
        ])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        layout.addWidget(self.table)

    def _load_tables(self):
        tables = self.config.get('tables', [])
        self.table.setRowCount(len(tables))
        for i, t in enumerate(tables):
            self._set_row(i, t)

    def _set_row(self, row: int, t: dict):
        # Table name
        self.table.setItem(row, 0, QTableWidgetItem(t.get('name', '')))

        # Mode combo
        mode_combo = QComboBox()
        mode_combo.addItems(TABLE_MODES)
        mode_combo.setCurrentText(t.get('mode', 'timeframe'))
        self.table.setCellWidget(row, 1, mode_combo)

        # Key fields
        self.table.setItem(row, 2, QTableWidgetItem(t.get('key_fields', '')))

        # Delta field
        self.table.setItem(row, 3, QTableWidgetItem(t.get('delta_field', '')))

        # Window combo
        window_combo = QComboBox()
        window_combo.addItems([''] + WINDOW_OPTIONS)
        window_combo.setCurrentText(t.get('window', ''))
        self.table.setCellWidget(row, 4, window_combo)

        # Target table
        self.table.setItem(row, 5, QTableWidgetItem(t.get('target_table', '')))

        # Replace mode combo
        replace_combo = QComboBox()
        replace_combo.addItems(REPLACE_MODES)
        replace_combo.setCurrentText(t.get('replace_mode', 'append'))
        self.table.setCellWidget(row, 6, replace_combo)

        # Chunk size
        chunk_spin = QSpinBox()
        chunk_spin.setRange(1000, 100000)
        chunk_spin.setValue(t.get('chunk_size', 10000))
        chunk_spin.setSingleStep(1000)
        self.table.setCellWidget(row, 7, chunk_spin)

        # Fields
        self.table.setItem(row, 8, QTableWidgetItem(t.get('fields', '*')))

        # Active checkbox
        active_check = QCheckBox()
        active_check.setChecked(t.get('active', True))
        self.table.setCellWidget(row, 9, active_check)

    def _add_table(self):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._set_row(row, {
            'name': 'NEW_TABLE',
            'mode': 'timeframe',
            'key_fields': '',
            'delta_field': 'AEDAT',
            'window': 'month',
            'target_table': '',
            'replace_mode': 'append',
            'chunk_size': 10000,
            'fields': '*',
            'active': True
        })

    def _remove_table(self):
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def _context_menu(self, pos):
        menu = QMenu(self)
        action_add = menu.addAction("Duplizieren")
        action_del = menu.addAction("Entfernen")
        action = menu.exec_(self.table.mapToGlobal(pos))
        row = self.table.rowAt(pos.y())
        if action == action_add and row >= 0:
            self._duplicate_row(row)
        elif action == action_del and row >= 0:
            self.table.removeRow(row)

    def _duplicate_row(self, row: int):
        t = self._read_row(row)
        t['name'] = t['name'] + '_COPY'
        new_row = self.table.rowCount()
        self.table.insertRow(new_row)
        self._set_row(new_row, t)

    def _read_row(self, row: int) -> dict:
        def get_text(col):
            item = self.table.item(row, col)
            return item.text() if item else ''

        def get_combo(col):
            widget = self.table.cellWidget(row, col)
            if isinstance(widget, QComboBox):
                return widget.currentText()
            return ''

        def get_spin(col):
            widget = self.table.cellWidget(row, col)
            if isinstance(widget, QSpinBox):
                return widget.value()
            return 0

        def get_check(col):
            widget = self.table.cellWidget(row, col)
            if isinstance(widget, QCheckBox):
                return widget.isChecked()
            return True

        return {
            'name': get_text(0),
            'mode': get_combo(1),
            'key_fields': get_text(2),
            'delta_field': get_text(3),
            'window': get_combo(4),
            'target_table': get_text(5) or None,
            'replace_mode': get_combo(6),
            'chunk_size': get_spin(7),
            'fields': get_text(8) or '*',
            'active': get_check(9)
        }

    def _save(self):
        tables = []
        for i in range(self.table.rowCount()):
            t = self._read_row(i)
            if t['name'] and t['name'] != 'NEW_TABLE':
                tables.append(t)

        self.config['tables'] = tables
        try:
            self.config_manager.set(self.config)
            QMessageBox.information(self, "Gespeichert", f"{len(tables)} Tabellen gespeichert.")
            self.config_changed.emit()
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Speichern fehlgeschlagen: {e}")

    def get_active_tables(self) -> list:
        tables = []
        for i in range(self.table.rowCount()):
            t = self._read_row(i)
            if t.get('active', True) and t['name'] and t['name'] != 'NEW_TABLE':
                tables.append(t)
        return tables

    def get_selected_table(self) -> Optional[dict]:
        row = self.table.currentRow()
        if row >= 0:
            return self._read_row(row)
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
        self.log_output.append(f'<span style="color:{color}">{message}</span>')

    def _set_progress(self, table_name: str, status: str):
        # Find or create row
        for i in range(self.progress_table.rowCount()):
            if self.progress_table.item(i, 0).text() == table_name:
                self.progress_table.item(i, 1).setText(status)
                self.progress_table.item(i, 2).setText(datetime.now().strftime('%H:%M:%S'))
                # Color the status
                if '✓' in status:
                    self.progress_table.item(i, 1).setForeground(QColor('#008800'))
                elif '✗' in status or 'Error' in status:
                    self.progress_table.item(i, 1).setForeground(QColor('#CC0000'))
                elif 'Running' in status:
                    self.progress_table.item(i, 1).setForeground(QColor('#0066CC'))
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
        QMessageBox.information(self, "Fertig",
            f"Sync abgeschlossen.\n{success} erfolgreich, {fail} fehlgeschlagen.")


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
        schedules = []
        for i in range(self.sched_table.rowCount()):
            s = self._read_sched_row(i)
            if s['table']:
                schedules.append(s)

        config = self.config_manager.get()
        config['schedules'] = schedules
        try:
            self.config_manager.set(config)
            QMessageBox.information(self, "Gespeichert", f"{len(schedules)} Jobs gespeichert.")
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Speichern fehlgeschlagen: {e}")

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
        # Save first
        self._save_schedules()

        config = self.config_manager.get()
        schedules = config.get('schedules', [])
        active = [s for s in schedules if s.get('active', True)]
        if not active:
            QMessageBox.warning(self, "Keine Jobs", "Keine aktiven Jobs im Scheduler.")
            return

        self.scheduler_running = True
        self.last_run_time = {}

        # Timer for checking schedules every 60 seconds
        from PySide6.QtCore import QTimer
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

        import time
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
                    if t['name'] == table:
                        table_cfg = t
                        break

                if table_cfg:
                    self.run_tab._log("INFO",
                        f"Scheduler: starte {action} für {table} (Intervall: {interval})")

                    if action == 'sync':
                        self.run_tab._start_worker([table_cfg], "sync")
                    elif action == 'sync_schema':
                        self.run_tab._start_worker([table_cfg], "sync_schema")
                    elif action == 'init_only':
                        self.run_tab._start_worker([table_cfg], "init_only")

                    self.last_run_time[table] = now
                    break  # Only one job at a time

    def _export_windows_task(self):
        import subprocess

        task_name = self.task_name.text().strip()
        if not task_name:
            QMessageBox.warning(self, "Fehler", "Aufgabenname darf nicht leer sein.")
            return

        interval = self.task_interval.currentText()
        mode = self.task_mode.currentText()
        start_time = self.task_time.text().strip()
        run_user = self.task_user.text().strip().upper()
        run_password = self.task_password.text()
        highest_priv = self.task_highest.isChecked()
        run_background = self.task_run_background.isChecked()

        # Get paths
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
        else:
            cli_args += ' --window day'

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

    def closeEvent(self, event):
        self._stop_scheduler()


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

        layout.addWidget(self.tabs)

        # Status Bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Bereit")

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

        action_about = QAction("Über", self)
        action_about.triggered.connect(self._show_about)
        help_menu.addAction(action_about)

    def _save_all(self):
        try:
            self.settings_tab._save()
            self.tables_tab._save()
            self.status_bar.showMessage("Konfiguration gespeichert", 3000)
        except Exception as e:
            QMessageBox.critical(self, "Fehler", f"Speichern fehlgeschlagen: {e}")

    def _reload_config(self):
        self.config_manager.load()
        self.settings_tab.config = self.config_manager.get()
        self.settings_tab._load_values()
        self.tables_tab.config = self.config_manager.get()
        self.tables_tab._load_tables()
        self.status_bar.showMessage("Konfiguration neu geladen", 3000)

    def _show_about(self):
        QMessageBox.about(self, "Über SAP Data Replication",
            "<h3>SAP Data Replication Client</h3>"
            "<p>Replikation von SAP-Tabellen in Microsoft SQL Server</p>"
            "<p><b>Modi:</b> CDC (Trigger), Timeframe, Full-Load, Flatfile</p>"
            "<p><b>Lizenz:</b> GPL-3.0</p>"
            "<p><b>GitHub:</b> https://github.com/vbkredeemer/sap-data-replication</p>")

    def closeEvent(self, event):
        if hasattr(self, 'run_tab') and self.run_tab.worker and self.run_tab.worker.isRunning():
            reply = QMessageBox.question(self, "Vorgang läuft",
                "Ein Sync-Vorgang läuft noch. Trotzdem beenden?")
            if reply != QMessageBox.Yes:
                event.ignore()
                return
            self.run_tab.worker.cancel()
            self.run_tab.worker.wait(5000)
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