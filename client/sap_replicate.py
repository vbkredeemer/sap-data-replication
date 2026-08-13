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

    def _get_window_start(self, window: str) -> str:
        """Calculate the start date for the given window."""
        today = date.today()
        if window == 'day':
            return today.strftime('%Y%m%d')
        elif window == 'week':
            from datetime import timedelta
            start = today - timedelta(days=today.weekday())
            return start.strftime('%Y%m%d')
        elif window == 'month':
            return today.strftime('%Y%m01')
        elif window == 'year':
            return today.strftime('%Y0101')
        else:
            return today.strftime('%Y%m%d')

    def sync_table(self, table: str, delta_field: str, window: str = 'month',
                   chunk_size: int = 10000):
        """Sync a table using timeframe delta (trigger-free)."""
        window_start = self._get_window_start(window)
        log.info(f"TIMEFRAME sync: {table} where {delta_field} >= {window_start} (window={window})")

        # Step 1: Delete current window from target
        log.info(f"  Deleting rows from dbo.{table} where {delta_field} >= '{window_start}'")
        self.sql.execute(
            f"DELETE FROM dbo.{table} WHERE {delta_field} >= ?",
            (window_start,)
        )
        self.sql.commit()

        # Step 2: Read from SAP via Z_READ_TABLE with WHERE clause
        where_clause = f"{delta_field} >= '{window_start}'"
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
# Main
# ============================================================================

def load_config(path: str) -> dict:
    with open(path, 'r') as f:
        return json.load(f)


def run_table(table_cfg: dict, sap: SapConnection, sql: SqlServerConnection,
              state: StateManager, mode_override: str = None, window_override: str = None):
    """Run sync for a single table based on its config."""

    table = table_cfg['name']
    mode = mode_override or table_cfg.get('mode', 'timeframe')
    key_fields = table_cfg.get('key_fields', '')
    delta_field = table_cfg.get('delta_field', 'AEDAT')
    window = window_override or table_cfg.get('window', 'month')
    chunk_size = table_cfg.get('chunk_size', 10000)

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
    parser.add_argument('--mode', choices=['cdc', 'timeframe', 'full'],
                        help='Override mode for --table')
    parser.add_argument('--window', choices=['day', 'week', 'month', 'year'],
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
            ok = run_table(t, sap, sql, state, args.mode, args.window)
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