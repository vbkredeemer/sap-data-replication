#!/usr/bin/env python3
"""
SAP Data Replication Client
============================
Repliziert SAP-Tabellen in eine Zieldatenbank (Microsoft SQL Server).

Zwei Modi:
  1. CDC-Modus (trigger-basiert): Z_CDC_INIT → Z_CDC_READ → Z_CDC_CLEANUP
  2. Zeitfenster-Modus (trigger-frei): DELETE Zeitraum → Z_READ_TABLE → INSERT

Voraussetzungen:
  - pyrfc (pip install pyrfc) + SAP NWRFC SDK (libsapnwrfc.dll/.so)
  - pyodbc (pip install pyodbc) + ODBC Driver for SQL Server
  - SAP-Funktionsbausteine: Z_CDC_INIT, Z_CDC_READ, Z_CDC_CLEANUP, Z_READ_TABLE
  - DDIC-Typen: ZSQL_FIELD, ZSQL_ROW (aus dem ODBC-Projekt)

Usage:
  python sap_replicate.py --config config.json
  python sap_replicate.py --config config.json --table MARA --mode cdc
  python sap_replicate.py --config config.json --table ACDOCA --mode timeframe --window day
"""

import argparse
import json
import logging
import sys
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple

try:
    from pyrfc import Connection
except ImportError:
    print("ERROR: pyrfc not installed. Install with: pip install pyrfc")
    print("       Also requires SAP NWRFC SDK (libsapnwrfc.dll/.so)")
    sys.exit(1)

try:
    import pyodbc
except ImportError:
    print("ERROR: pyodbc not installed. Install with: pip install pyodbc")
    sys.exit(1)


# ============================================================================
# Logging
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('sap_replicate')


# ============================================================================
# SAP Connection
# ============================================================================

class SapConnection:
    """Wraps pyrfc Connection with error handling."""

    def __init__(self, config: dict):
        self.config = config
        self.conn = None

    def connect(self):
        try:
            self.conn = Connection(
                ashost=self.config['ashost'],
                sysnr=self.config['sysnr'],
                client=self.config['client'],
                user=self.config['user'],
                passwd=self.config['password'],
                lang=self.config.get('lang', 'EN')
            )
            log.info(f"Connected to SAP {self.config['ashost']}:{self.config['sysnr']} client {self.config['client']}")
        except Exception as e:
            log.error(f"Cannot connect to SAP: {e}")
            raise

    def call(self, func_name: str, **params) -> dict:
        try:
            return self.conn.call(func_name, **params)
        except Exception as e:
            log.error(f"RFC call {func_name} failed: {e}")
            raise

    def close(self):
        if self.conn:
            self.conn.close()
            log.info("SAP connection closed")


# ============================================================================
# SQL Server Connection
# ============================================================================

class SqlServerConnection:
    """Wraps pyodbc for SQL Server operations."""

    def __init__(self, config: dict):
        self.config = config
        self.conn = None

    def connect(self):
        try:
            self.conn = pyodbc.connect(self.config['connection_string'])
            log.info("Connected to SQL Server")
        except Exception as e:
            log.error(f"Cannot connect to SQL Server: {e}")
            raise

    def execute(self, sql: str, params=None):
        cursor = self.conn.cursor()
        if params:
            cursor.execute(sql, params)
        else:
            cursor.execute(sql)
        return cursor

    def executemany(self, sql: str, rows: list):
        cursor = self.conn.cursor()
        cursor.fast_executemany = True
        cursor.executemany(sql, rows)
        return cursor.rowcount

    def commit(self):
        self.conn.commit()

    def close(self):
        if self.conn:
            self.conn.close()
            log.info("SQL Server connection closed")


# ============================================================================
# State Management (stores last SEQ per table for CDC)
# ============================================================================

class StateManager:
    """Manages CDC state (last SEQ) in the target database."""

    def __init__(self, sql: SqlServerConnection):
        self.sql = sql
        self._ensure_table()

    def _ensure_table(self):
        self.sql.execute("""
            IF NOT EXISTS (SELECT * FROM sys.tables WHERE name = 'CDC_STATE')
            CREATE TABLE CDC_STATE (
                table_name NVARCHAR(128) PRIMARY KEY,
                last_seq INT NOT NULL DEFAULT 0,
                last_sync DATETIME2 NULL,
                mode NVARCHAR(20) NULL
            )
        """)
        self.sql.commit()

    def get_last_seq(self, table: str) -> int:
        cursor = self.sql.execute(
            "SELECT ISNULL(last_seq, 0) FROM CDC_STATE WHERE table_name = ?", (table,)
        )
        row = cursor.fetchone()
        return row[0] if row else 0

    def set_last_seq(self, table: str, seq: int, mode: str):
        self.sql.execute("""
            MERGE CDC_STATE AS target
            USING (SELECT ? AS table_name, ? AS last_seq, ? AS mode) AS source
            ON target.table_name = source.table_name
            WHEN MATCHED THEN UPDATE SET last_seq = source.last_seq, last_sync = GETDATE(), mode = source.mode
            WHEN NOT MATCHED THEN INSERT (table_name, last_seq, last_sync, mode) VALUES (source.table_name, source.last_seq, GETDATE(), source.mode);
        """, (table, seq, mode))
        self.sql.commit()


# ============================================================================
# CDC Mode (trigger-based)
# ============================================================================

class CdcReplicator:
    """Trigger-based CDC replication using Z_CDC_INIT, Z_CDC_READ, Z_CDC_CLEANUP."""

    def __init__(self, sap: SapConnection, sql: SqlServerConnection, state: StateManager):
        self.sap = sap
        self.sql = sql
        self.state = state

    def init_table(self, table: str, key_fields: str) -> dict:
        """Initialize CDC for a table. Idempotent — safe to call repeatedly."""
        log.info(f"CDC INIT: {table} (keys: {key_fields})")
        result = self.sap.call('Z_CDC_INIT',
                               IV_TABLE=table,
                               IV_KEYFIELDS=key_fields)

        if result.get('EV_ERROR'):
            log.error(f"CDC INIT error for {table}: {result['EV_ERROR']}")
            return result

        if result.get('EV_TRIGGER_EXISTS') == 'X':
            log.info(f"  Trigger already exists for {table}")
        else:
            log.info(f"  Trigger created for {table}")

        if result.get('EV_GAP_DETECTED') == 'X':
            log.warning(f"  GAP DETECTED for {table}! Last log entry was at {result.get('EV_LAST_LOG_TIME')}")
            log.warning(f"  A full reload of {table} is recommended!")
            result['_needs_full_load'] = True
        else:
            result['_needs_full_load'] = False

        return result

    def read_delta(self, table: str, from_seq: int, chunk_size: int = 10000) -> Tuple[List[str], int, bool]:
        """Read delta entries from SAP. Returns (rows, next_seq, has_more)."""
        result = self.sap.call('Z_CDC_READ',
                               IV_TABLE=table,
                               IV_FROM_SEQ=from_seq,
                               IV_CHUNK_SIZE=chunk_size)

        if result.get('EV_ERROR'):
            raise RuntimeError(f"Z_CDC_READ error: {result['EV_ERROR']}")

        rows = [row['ROWDATA'] for row in result.get('ET_DATA', [])]
        next_seq = result.get('EV_NEXT_SEQ', from_seq)
        has_more = result.get('EV_HAS_MORE') == 'X'

        return rows, next_seq, has_more

    def apply_delta(self, table: str, rows: List[str], key_fields: List[str]):
        """Apply delta rows to SQL Server. Each row: OPERATION|field1|field2|..."""
        if not rows:
            return 0

        # Determine column names from first row's field count
        # We need the column list — get from a simple SELECT on target table
        cursor = self.sql.execute(f"SELECT TOP 0 * FROM dbo.{table}")
        col_names = [desc[0] for desc in cursor.description]

        inserts = []
        updates = []
        deletes = []

        for rowdata in rows:
            parts = rowdata.split('|')
            operation = parts[0]
            values = parts[1:]

            if operation == 'D':
                # DELETE
                key_vals = values[:len(key_fields)]
                where = ' AND '.join(f"{k} = ?" for k in key_fields)
                self.sql.execute(f"DELETE FROM dbo.{table} WHERE {where}", key_vals)
            elif operation in ('I', 'U'):
                # INSERT or UPDATE (UPSERT via MERGE)
                if len(values) >= len(col_names):
                    vals = values[:len(col_names)]
                else:
                    vals = values + [''] * (len(col_names) - len(values))

                if operation == 'I':
                    placeholders = ','.join(['?'] * len(col_names))
                    col_list = ','.join(col_names)
                    self.sql.execute(
                        f"INSERT INTO dbo.{table} ({col_list}) VALUES ({placeholders})",
                        vals
                    )
                else:  # U
                    set_clause = ','.join(f"{c} = ?" for c in col_names if c not in key_fields)
                    where = ' AND '.join(f"{k} = ?" for k in key_fields)
                    update_vals = [v for c, v in zip(col_names, vals) if c not in key_fields]
                    update_vals += [vals[col_names.index(k)] for k in key_fields]
                    self.sql.execute(
                        f"UPDATE dbo.{table} SET {set_clause} WHERE {where}",
                        update_vals
                    )

        self.sql.commit()
        return len(rows)

    def cleanup(self, table: str, up_to_seq: int):
        """Clean up log entries after successful sync."""
        log.info(f"CDC CLEANUP: {table} up to SEQ {up_to_seq}")
        result = self.sap.call('Z_CDC_CLEANUP',
                               IV_TABLE=table,
                               IV_UP_TO_SEQ=up_to_seq,
                               IV_REMOVE_ALL=' ')
        if result.get('EV_ERROR'):
            log.warning(f"  Cleanup warning: {result['EV_ERROR']}")
        else:
            log.info(f"  Deleted {result.get('EV_DELETED', 0)} log entries")

    def remove_cdc(self, table: str):
        """Remove CDC completely (triggers, log table, sequence)."""
        log.info(f"CDC REMOVE: {table}")
        result = self.sap.call('Z_CDC_CLEANUP',
                               IV_TABLE=table,
                               IV_UP_TO_SEQ=0,
                               IV_REMOVE_ALL='X')
        if result.get('EV_ERROR'):
            log.error(f"  Remove error: {result['EV_ERROR']}")
        else:
            log.info(f"  CDC fully removed for {table}")

    def sync_table(self, table: str, key_fields: str, chunk_size: int = 10000):
        """Full sync cycle: init → read → apply → cleanup."""
        key_field_list = [k.strip() for k in key_fields.split(',')]

        # Step 1: Init (check trigger, detect gaps)
        init_result = self.init_table(table, key_fields)

        if init_result.get('EV_ERROR'):
            return False

        # Step 2: If gap detected, recommend full load
        if init_result.get('_needs_full_load'):
            log.warning(f"Gap detected for {table} — skipping delta sync. Full load needed.")
            return False

        # Step 3: Read delta in chunks
        last_seq = self.state.get_last_seq(table)
        total_rows = 0
        max_seq = last_seq

        while True:
            rows, next_seq, has_more = self.read_delta(table, last_seq, chunk_size)
            if rows:
                applied = self.apply_delta(table, rows, key_field_list)
                total_rows += applied
                log.info(f"  {table}: applied {applied} rows (total: {total_rows})")
            max_seq = next_seq - 1
            last_seq = next_seq
            if not has_more:
                break

        # Step 4: Save state
        self.state.set_last_seq(table, max_seq, 'cdc')

        # Step 5: Cleanup log
        if total_rows > 0:
            self.cleanup(table, max_seq)

        log.info(f"CDC sync complete: {table} — {total_rows} rows applied")
        return True


# ============================================================================
# Timeframe Delta Mode (trigger-free)
# ============================================================================

class TimeframeReplicator:
    """Timeframe-based delta replication using Z_READ_TABLE."""

    def __init__(self, sap: SapConnection, sql: SqlServerConnection):
        self.sap = sap
        self.sql = sql

    def _get_window_range(self, window: str) -> Tuple[str, str]:
        """
        Calculate the date range covering the current AND previous period.
        
        Returns (from_date, to_date) as YYYYMMDD strings.
        For 'all': returns ('', '') meaning no date filter — full table load.
        to_date is always today (inclusive).
        from_date is the start of the PREVIOUS period.
        """
        today = date.today()
        
        if window == 'all':
            # Full table — no date filter
            return '', ''
        
        if window == 'day':
            # Current: today, Previous: yesterday
            from datetime import timedelta
            prev = today - timedelta(days=1)
            return prev.strftime('%Y%m%d'), today.strftime('%Y%m%d')
            
        elif window == 'week':
            # Week = Monday to Sunday
            # Current week start (Monday)
            from datetime import timedelta
            current_week_start = today - timedelta(days=today.weekday())
            # Previous week start
            prev_week_start = current_week_start - timedelta(days=7)
            return prev_week_start.strftime('%Y%m%d'), today.strftime('%Y%m%d')
            
        elif window == 'month':
            # Current month start
            current_month_start = today.replace(day=1)
            # Previous month start
            if today.month == 1:
                prev_month_start = today.replace(year=today.year - 1, month=12, day=1)
            else:
                prev_month_start = today.replace(month=today.month - 1, day=1)
            return prev_month_start.strftime('%Y%m%d'), today.strftime('%Y%m%d')
            
        elif window == 'year':
            # Current year start
            current_year_start = today.replace(month=1, day=1)
            # Previous year start
            prev_year_start = current_year_start.replace(year=today.year - 1)
            return prev_year_start.strftime('%Y%m%d'), today.strftime('%Y%m%d')
            
        else:
            # Default: today + yesterday
            from datetime import timedelta
            prev = today - timedelta(days=1)
            return prev.strftime('%Y%m%d'), today.strftime('%Y%m%d')

    def sync_table(self, table: str, delta_field: str, window: str = 'month',
                   chunk_size: int = 10000):
        """Sync a table using timeframe delta (trigger-free).
        
        Always loads current + previous period to prevent data loss at boundaries.
        Deletes the same range in the target table before inserting.
        """
        date_from, date_to = self._get_window_range(window)
        
        if window == 'all' or not date_from:
            # Full table replace — TRUNCATE + load all
            log.info(f"TIMEFRAME sync: {table} (window=all → full table replace)")
            self.sql.execute(f"TRUNCATE TABLE dbo.{table}")
            self.sql.commit()
            where_clause = ""  # no WHERE filter
        else:
            log.info(f"TIMEFRAME sync: {table} where {delta_field} >= '{date_from}' "
                     f"(window={window}, current+previous period)")

            # Step 1: Delete current+previous period from target
            # Convert YYYYMMDD to YYYY-MM-DD for MSSQL DATE columns
            mssql_date_from = f"{date_from[:4]}-{date_from[4:6]}-{date_from[6:8]}"
            log.info(f"  Deleting rows from dbo.{table} where {delta_field} >= '{mssql_date_from}'")
            self.sql.execute(
                f"DELETE FROM dbo.{table} WHERE [{delta_field}] >= ?",
                (mssql_date_from,)
            )
            self.sql.commit()

            # Step 2: Read from SAP via Z_READ_TABLE with WHERE clause
            # SAP expects YYYYMMDD format (not YYYY-MM-DD)
            where_clause = f"{delta_field} >= '{date_from}'"
        total_rows = 0
        skip = 0

        # Get column metadata from first chunk
        col_names = None

        while True:
            result = self.sap.call('Z_READ_TABLE',
                                   IV_TABLE=table,
                                   IV_WHERE=where_clause,
                                   IV_FIELDS='*',
                                   IV_ORDERBY='',
                                   IV_ROWSKIPS=skip,
                                   IV_ROWCOUNT=chunk_size)

            if result.get('EV_ERROR'):
                log.error(f"  Z_READ_TABLE error: {result['EV_ERROR']}")
                return False

            data_rows = result.get('ET_DATA', [])
            if not data_rows:
                break

            # Get column names from target table (first call)
            if col_names is None:
                cursor = self.sql.execute(f"SELECT TOP 0 * FROM dbo.{table}")
                col_names = [desc[0] for desc in cursor.description]

            # Build INSERT batch
            placeholders = ','.join(['?'] * len(col_names))
            col_list = ','.join(col_names)
            insert_sql = f"INSERT INTO dbo.{table} ({col_list}) VALUES ({placeholders})"

            batch = []
            for row in data_rows:
                values = row['ROWDATA'].split('|')
                if len(values) >= len(col_names):
                    vals = values[:len(col_names)]
                else:
                    vals = values + [''] * (len(col_names) - len(values))
                batch.append(vals)

            self.sql.executemany(insert_sql, batch)
            self.sql.commit()

            total_rows += len(data_rows)
            skip += chunk_size
            log.info(f"  {table}: loaded {len(data_rows)} rows (total: {total_rows})")

            if result.get('EV_HAS_MORE') != 'X':
                break

        log.info(f"TIMEFRAME sync complete: {table} — {total_rows} rows loaded")
        return True


# ============================================================================
# Full Load Mode
# ============================================================================

class FullLoadReplicator:
    """Full table load using Z_READ_TABLE with chunking."""

    def __init__(self, sap: SapConnection, sql: SqlServerConnection):
        self.sap = sap
        self.sql = sql

    def sync_table(self, table: str, chunk_size: int = 10000):
        """Full load of a table."""
        log.info(f"FULL LOAD: {table}")

        # Step 1: Truncate target table
        log.info(f"  Truncating dbo.{table}")
        self.sql.execute(f"TRUNCATE TABLE dbo.{table}")
        self.sql.commit()

        # Step 2: Read all data via Z_READ_TABLE
        total_rows = 0
        skip = 0
        col_names = None

        while True:
            result = self.sap.call('Z_READ_TABLE',
                                   IV_TABLE=table,
                                   IV_WHERE='',
                                   IV_FIELDS='*',
                                   IV_ORDERBY='',
                                   IV_ROWSKIPS=skip,
                                   IV_ROWCOUNT=chunk_size)

            if result.get('EV_ERROR'):
                log.error(f"  Z_READ_TABLE error: {result['EV_ERROR']}")
                return False

            data_rows = result.get('ET_DATA', [])
            if not data_rows:
                break

            if col_names is None:
                cursor = self.sql.execute(f"SELECT TOP 0 * FROM dbo.{table}")
                col_names = [desc[0] for desc in cursor.description]

            placeholders = ','.join(['?'] * len(col_names))
            col_list = ','.join(col_names)
            insert_sql = f"INSERT INTO dbo.{table} ({col_list}) VALUES ({placeholders})"

            batch = []
            for row in data_rows:
                values = row['ROWDATA'].split('|')
                if len(values) >= len(col_names):
                    vals = values[:len(col_names)]
                else:
                    vals = values + [''] * (len(col_names) - len(values))
                batch.append(vals)

            self.sql.executemany(insert_sql, batch)
            self.sql.commit()

            total_rows += len(data_rows)
            skip += chunk_size

            if total_rows % 100000 == 0:
                log.info(f"  {table}: loaded {total_rows} rows...")

            if result.get('EV_HAS_MORE') != 'X':
                break

        log.info(f"FULL LOAD complete: {table} — {total_rows} rows")
        return True


# ============================================================================
# Flatfile Export Mode (fastest for large tables)
# ============================================================================

class FlatfileReplicator:
    """Flatfile-based replication: Z_EXPORT_TABLE → download → BULK INSERT.

    Download methods (configured per table or globally):
      - 'scp':  SCP over SSH (for Linux SAP servers without Samba)
      - 'smb':  Windows network share (UNC path, e.g. \\sap-server\sap\tmp\)
      - 'local': File is already accessible on local filesystem (e.g. mounted NFS)
    """

    def __init__(self, sap: SapConnection, sql: SqlServerConnection,
                 ssh_config: dict = None, transfer_method: str = 'scp',
                 smb_share: str = None):
        self.sap = sap
        self.sql = sql
        self.ssh_config = ssh_config
        self.transfer_method = transfer_method  # 'scp', 'smb', 'local'
        self.smb_share = smb_share  # e.g. r'\\sap-server\sap\tmp'

    def _get_window_range(self, window: str) -> Tuple[str, str]:
        """
        Calculate date range covering current AND previous period.
        Overlap prevents data loss at period boundaries.
        
        Returns (from_date, to_date) as YYYYMMDD strings.
        from_date = start of PREVIOUS period.
        to_date = today (inclusive).
        """
        today = date.today()
        
        if window == 'day':
            from datetime import timedelta
            prev = today - timedelta(days=1)
            return prev.strftime('%Y%m%d'), today.strftime('%Y%m%d')
        elif window == 'week':
            from datetime import timedelta
            current_week_start = today - timedelta(days=today.weekday())
            prev_week_start = current_week_start - timedelta(days=7)
            return prev_week_start.strftime('%Y%m%d'), today.strftime('%Y%m%d')
        elif window == 'month':
            if today.month == 1:
                prev_month_start = today.replace(year=today.year - 1, month=12, day=1)
            else:
                prev_month_start = today.replace(month=today.month - 1, day=1)
            return prev_month_start.strftime('%Y%m%d'), today.strftime('%Y%m%d')
        elif window == 'year':
            prev_year_start = today.replace(year=today.year - 1, month=1, day=1)
            return prev_year_start.strftime('%Y%m%d'), today.strftime('%Y%m%d')
        else:
            from datetime import timedelta
            prev = today - timedelta(days=1)
            return prev.strftime('%Y%m%d'), today.strftime('%Y%m%d')

    def _download_file(self, remote_path: str, local_path: str) -> bool:
        """Download file from SAP server — supports SCP, SMB, and local methods."""
        import shutil

        if self.transfer_method == 'smb':
            return self._download_smb(remote_path, local_path)
        elif self.transfer_method == 'local':
            return self._download_local(remote_path, local_path)
        else:
            return self._download_scp(remote_path, local_path)

    def _download_scp(self, remote_path: str, local_path: str) -> bool:
        """Download file via SCP over SSH."""
        if not self.ssh_config:
            log.error("  No SSH/SCP config — cannot download file via SCP")
            return False

        import subprocess
        host = self.ssh_config['host']
        user = self.ssh_config.get('user', '')
        key = self.ssh_config.get('key_file', '')
        port = self.ssh_config.get('port', 22)

        cmd = ['scp', '-P', str(port)]
        if key:
            cmd.extend(['-i', key])
        cmd.append(f"{user}@{host}:{remote_path}" if user else f"{host}:{remote_path}")
        cmd.append(local_path)

        try:
            log.info(f"  Downloading via SCP: {remote_path} → {local_path}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            if result.returncode != 0:
                log.error(f"  SCP failed: {result.stderr}")
                return False
            log.info(f"  SCP download complete")
            return True
        except Exception as e:
            log.error(f"  SCP error: {e}")
            return False

    def _download_smb(self, remote_path: str, local_path: str) -> bool:
        """Download file via SMB/Windows share (UNC path).
        
        remote_path is the Unix path on the SAP server (e.g. /usr/sap/tmp/file.csv).
        The smb_share config maps this to a UNC path (e.g. \\sap-server\sap\tmp\).
        We extract the filename and build the UNC path.
        """
        import shutil
        import os

        if not self.smb_share:
            log.error("  No SMB share configured — cannot download via SMB")
            return False

        # Extract filename from remote path
        filename = os.path.basename(remote_path)

        # Build UNC path: \\server\share\filename
        # smb_share should be like \\sap-server\sap\tmp (no trailing backslash)
        smb_path = os.path.join(self.smb_share, filename)

        try:
            log.info(f"  Downloading via SMB: {smb_path} → {local_path}")
            shutil.copy2(smb_path, local_path)
            log.info(f"  SMB download complete")
            return True
        except FileNotFoundError:
            log.error(f"  File not found on SMB share: {smb_path}")
            return False
        except PermissionError:
            log.error(f"  Permission denied accessing SMB share: {smb_path}")
            return False
        except Exception as e:
            log.error(f"  SMB download error: {e}")
            return False

    def _download_local(self, remote_path: str, local_path: str) -> bool:
        """File is already accessible on local filesystem (NFS mount, etc.).
        
        remote_path is directly accessible from the Python client.
        Just copy it to the temp location.
        """
        import shutil
        import os

        if not os.path.exists(remote_path):
            log.error(f"  File not found locally: {remote_path}")
            return False

        try:
            log.info(f"  Copying local file: {remote_path} → {local_path}")
            shutil.copy2(remote_path, local_path)
            log.info(f"  Local copy complete")
            return True
        except Exception as e:
            log.error(f"  Local copy error: {e}")
            return False

    def _bulk_insert_csv(self, csv_path: str, target_table: str,
                         replace_mode: str = 'append',
                         date_field: str = None, date_from: str = None,
                         date_to: str = None) -> int:
        """Bulk insert CSV into SQL Server using BULK INSERT."""
        import os
        import csv

        # Read CSV header to get column names
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.reader(f, delimiter='|')
            header = next(reader)

        col_names = [h.strip() for h in header]
        col_list = ','.join(f"[{c}]" for c in col_names)

        # Replace mode: delete target rows before insert
        if replace_mode == 'replace_all':
            log.info(f"  TRUNCATE dbo.{target_table}")
            self.sql.execute(f"TRUNCATE TABLE dbo.{target_table}")
            self.sql.commit()
        elif replace_mode == 'replace_window' and date_field and date_from:
            # date_from is YYYYMMDD from _get_window_range — convert to YYYY-MM-DD for MSSQL
            mssql_date_from = f"{date_from[:4]}-{date_from[4:6]}-{date_from[6:8]}"
            where = f"WHERE [{date_field}] >= '{mssql_date_from}'"
            if date_to:
                mssql_date_to = f"{date_to[:4]}-{date_to[4:6]}-{date_to[6:8]}"
                where += f" AND [{date_field}] <= '{mssql_date_to}'"
            log.info(f"  DELETE FROM dbo.{target_table} {where}")
            self.sql.execute(f"DELETE FROM dbo.{target_table} {where}")
            self.sql.commit()

        # Use BULK INSERT via pyodbc (fastest method)
        # Need to share the file via a network path or local path
        # For local execution: use BULK INSERT directly
        csv_abs = os.path.abspath(csv_path).replace('\\', '\\\\')

        bulk_sql = f"""
            BULK INSERT dbo.{target_table}
            FROM '{csv_abs}'
            WITH (
                FORMAT = 'CSV',
                FIELDTERMINATOR = '|',
                ROWTERMINATOR = '\\n',
                FIRSTROW = 2,
                TABLOCK,
                ROWS_PER_BATCH = 50000
            )
        """

        try:
            log.info(f"  BULK INSERT into dbo.{target_table}")
            cursor = self.sql.execute(bulk_sql)
            self.sql.commit()

            # Get row count
            cursor = self.sql.execute(f"SELECT @@ROWCOUNT")
            row_count = cursor.fetchone()[0]
            log.info(f"  BULK INSERT complete: {row_count} rows")
            return row_count
        except Exception as e:
            log.error(f"  BULK INSERT failed: {e}")
            # Fallback: batch insert via pyodbc
            return self._batch_insert_csv(csv_path, target_table, col_names)

    def _batch_insert_csv(self, csv_path: str, target_table: str,
                          col_names: list) -> int:
        """Fallback: batch insert via pyodbc executemany."""
        import csv

        placeholders = ','.join(['?'] * len(col_names))
        col_list = ','.join(f"[{c}]" for c in col_names)
        insert_sql = f"INSERT INTO dbo.{target_table} ({col_list}) VALUES ({placeholders})"

        batch = []
        total = 0
        batch_size = 5000

        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.reader(f, delimiter='|')
            next(reader)  # skip header

            for row in reader:
                # Pad row if needed
                while len(row) < len(col_names):
                    row.append('')
                batch.append(row[:len(col_names)])

                if len(batch) >= batch_size:
                    self.sql.executemany(insert_sql, batch)
                    self.sql.commit()
                    total += len(batch)
                    log.info(f"    Inserted {total} rows...")
                    batch = []

            if batch:
                self.sql.executemany(insert_sql, batch)
                self.sql.commit()
                total += len(batch)

        log.info(f"  Batch insert complete: {total} rows")
        return total

    def sync_table(self, table: str, target_table: str = None,
                   date_field: str = None, date_from: str = None,
                   date_to: str = None, window: str = None,
                   fields: str = '*', max_rows: int = 0,
                   file_path: str = '/usr/sap/tmp/',
                   replace_mode: str = 'append',
                   cleanup_after: bool = True):
        """
        Sync a table via flatfile export.
        
        replace_mode:
          'append'        — just insert, don't delete
          'replace_all'   — TRUNCATE target before insert
          'replace_window' — DELETE date range in target before insert
        """

        target = target_table or table

        # Calculate date range from window if not explicitly set
        # Always includes current + previous period for overlap safety
        if window and not date_from:
            date_from, date_to = self._get_window_range(window)
            if window == 'all' or not date_from:
                # Full table — no date filter, replace all
                log.info(f"FLATFILE sync: {table} → dbo.{target} (window=all → full table replace)")
                replace_mode = 'replace_all'
                date_field = None  # no date filter for export
            else:
                log.info(f"FLATFILE sync: {table} → dbo.{target} "
                         f"(window={window}, current+previous: {date_from} to {date_to})")
                # When using window, always use replace_window to delete the same range
                if date_field:
                    replace_mode = 'replace_window'
        else:
            log.info(f"FLATFILE sync: {table} → dbo.{target}")

        # Step 1: Call Z_EXPORT_TABLE on SAP
        params = {
            'IV_TABLE': table,
            'IV_FIELDS': fields,
            'IV_MAX_ROWS': max_rows,
            'IV_FILE_PATH': file_path,
        }
        if date_field:
            params['IV_DATE_FIELD'] = date_field
        if date_from:
            params['IV_DATE_FROM'] = date_from
        if date_to:
            params['IV_DATE_TO'] = date_to

        log.info(f"  Calling Z_EXPORT_TABLE...")
        result = self.sap.call('Z_EXPORT_TABLE', **params)

        if result.get('EV_ERROR'):
            log.error(f"  Export error: {result['EV_ERROR']}")
            return False

        remote_file = result.get('EV_FILE_NAME', '')
        row_count = result.get('EV_ROW_COUNT', 0)
        file_size = result.get('EV_FILE_SIZE', 0)

        log.info(f"  Export complete: {row_count} rows, {file_size} bytes → {remote_file}")

        if row_count == 0:
            log.info(f"  No data to transfer")
            # Still cleanup the empty file
            if cleanup_after and remote_file:
                self.sap.call('Z_DELETE_FILE', IV_FILE_PATH=remote_file)
            return True

        # Step 2: Download file from SAP server
        import tempfile
        import os
        local_file = os.path.join(tempfile.gettempdir(), os.path.basename(remote_file))

        if not self._download_file(remote_file, local_file):
            log.error(f"  Cannot download file from SAP server")
            return False

        # Step 3: Bulk insert into SQL Server
        inserted = self._bulk_insert_csv(local_file, target,
                                          replace_mode=replace_mode,
                                          date_field=date_field,
                                          date_from=date_from,
                                          date_to=date_to)

        # Step 4: Cleanup
        if cleanup_after:
            # Delete remote file on SAP server
            log.info(f"  Cleaning up remote file: {remote_file}")
            self.sap.call('Z_DELETE_FILE', IV_FILE_PATH=remote_file)

            # Delete local file
            try:
                os.remove(local_file)
            except OSError:
                pass

        log.info(f"FLATFILE sync complete: {table} → {target} — {inserted} rows")
        return True


# ============================================================================
# Main
# ============================================================================

def load_config(path: str) -> dict:
    with open(path, 'r') as f:
        return json.load(f)


def run_table(table_cfg: dict, sap: SapConnection, sql: SqlServerConnection,
              state: StateManager, config: dict = None,
              mode_override: str = None, window_override: str = None):
    """Run sync for a single table based on its config."""

    table = table_cfg['name']
    mode = mode_override or table_cfg.get('mode', 'timeframe')
    key_fields = table_cfg.get('key_fields', '')
    delta_field = table_cfg.get('delta_field', 'AEDAT')
    window = window_override or table_cfg.get('window', 'month')
    chunk_size = table_cfg.get('chunk_size', 10000)
    target_table = table_cfg.get('target_table', table)
    date_field = table_cfg.get('date_field')
    replace_mode = table_cfg.get('replace_mode', 'append')
    file_path = table_cfg.get('file_path', '/usr/sap/tmp/')
    fields = table_cfg.get('fields', '*')
    max_rows = table_cfg.get('max_rows', 0)

    try:
        if mode == 'cdc':
            if not key_fields:
                log.error(f"  {table}: CDC mode requires key_fields in config")
                return False
            replicator = CdcReplicator(sap, sql, state)
            return replicator.sync_table(table, key_fields, chunk_size)

        elif mode == 'timeframe':
            replicator = TimeframeReplicator(sap, sql)
            return replicator.sync_table(table, delta_field, window, chunk_size)

        elif mode == 'full':
            replicator = FullLoadReplicator(sap, sql)
            return replicator.sync_table(table, chunk_size)

        elif mode == 'flatfile':
            ssh_config = config.get('ssh') if config else None
            flatfile_cfg = config.get('flatfile', {}) if config else {}
            transfer_method = flatfile_cfg.get('transfer_method', 'scp')
            smb_share = flatfile_cfg.get('smb_share', '')
            replicator = FlatfileReplicator(sap, sql, ssh_config,
                                             transfer_method=transfer_method,
                                             smb_share=smb_share)
            return replicator.sync_table(
                table=table,
                target_table=target_table,
                date_field=date_field if mode_override != 'flatfile' else delta_field,
                window=window,
                fields=fields,
                max_rows=max_rows,
                file_path=file_path,
                replace_mode=replace_mode if not window else 'replace_window' if date_field else 'replace_all'
            )

        else:
            log.error(f"  {table}: unknown mode '{mode}'")
            return False

    except Exception as e:
        log.error(f"  {table}: sync failed — {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description='SAP Data Replication Client')
    parser.add_argument('--config', required=True, help='Path to config.json')
    parser.add_argument('--table', help='Sync only this table (default: all in config)')
    parser.add_argument('--mode', choices=['cdc', 'timeframe', 'full', 'flatfile'],
                        help='Override mode for --table')
    parser.add_argument('--window', choices=['day', 'week', 'month', 'year', 'all'],
                        help='Override window for timeframe mode')
    parser.add_argument('--init-only', action='store_true',
                        help='Only run Z_CDC_INIT (check triggers, no sync)')
    parser.add_argument('--remove-cdc', metavar='TABLE',
                        help='Remove CDC for a table (triggers + log table)')
    args = parser.parse_args()

    config = load_config(args.config)

    # Connect
    sap = SapConnection(config['sap'])
    sql = SqlServerConnection(config['sql_server'])
    state = StateManager(sql)

    try:
        sap.connect()
        sql.connect()

        # Special commands
        if args.remove_cdc:
            cdc = CdcReplicator(sap, sql, state)
            cdc.remove_cdc(args.remove_cdc)
            return

        if args.init_only:
            tables = config['tables']
            if args.table:
                tables = [t for t in tables if t['name'] == args.table]
            cdc = CdcReplicator(sap, sql, state)
            for t in tables:
                if t.get('mode') == 'cdc' and t.get('key_fields'):
                    cdc.init_table(t['name'], t['key_fields'])
            return

        # Normal sync
        tables = config['tables']
        if args.table:
            tables = [t for t in tables if t['name'] == args.table]

        success_count = 0
        fail_count = 0

        for t in tables:
            log.info(f"--- Syncing {t['name']} ---")
            ok = run_table(t, sap, sql, state, config, args.mode, args.window)
            if ok:
                success_count += 1
            else:
                fail_count += 1

        log.info(f"=== Sync complete: {success_count} succeeded, {fail_count} failed ===")

    finally:
        sap.close()
        sql.close()


if __name__ == '__main__':
    main()