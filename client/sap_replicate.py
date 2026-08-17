#!/usr/bin/env python3
"""
SAP Data Replication Client
============================
Repliziert SAP-Tabellen in eine Zieldatenbank (Microsoft SQL Server).

Vier Modi:
  1. CDC-Modus (trigger-basiert): Z_CDC_INIT → Z_CDC_READ → Z_CDC_CLEANUP
  2. Zeitfenster-Modus (trigger-frei): DELETE Zeitraum → Z_READ_TABLE → INSERT
  3. Full-Load-Modus: TRUNCATE → Z_READ_TABLE → INSERT
  4. Flatfile-Modus: Z_EXPORT_TABLE → SCP/SMB → BULK INSERT

Voraussetzungen:
  - sap_rfc.py (included, pure-Python ctypes wrapper) + SAP NWRFC SDK (sapnwrfc.dll/.so)
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
import os
import re
import shutil
import sys
from datetime import datetime, date, timedelta
from typing import List, Tuple

# NWRFC DLL bootstrap — must run before sap_rfc is imported
from nwrfc_bootstrap import bootstrap as _nwrfc_bootstrap
_nwrfc_status, _nwrfc_msg = _nwrfc_bootstrap()
if _nwrfc_status == "missing":
    print(f"ERROR: {_nwrfc_msg}", file=sys.stderr)
    sys.exit(1)
if _nwrfc_status == "elevated":
    sys.exit(0)

try:
    from sap_rfc import Connection
except ImportError:
    print("ERROR: sap_rfc module not found. It should be in the same directory.")
    print("       Also requires SAP NWRFC SDK (sapnwrfc.dll/.so)")
    sys.exit(1)

try:
    import pyodbc
except ImportError:
    print("ERROR: pyodbc not installed. Install with: pip install pyodbc")
    sys.exit(1)


# ============================================================================
# Logging — console + file
# ============================================================================

_log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
os.makedirs(_log_dir, exist_ok=True)
_log_file = os.path.join(_log_dir, f'sap_replicate_{datetime.now().strftime("%Y%m%d")}.log')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_log_file, encoding='utf-8'),
    ]
)
log = logging.getLogger('sap_replicate')


# ============================================================================
# Helpers
# ============================================================================

_VALID_TABLE_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_/]{0,30}$')

def _validate_table_name(name: str) -> str:
    """Validate and sanitize a table name for use in SQL."""
    if not name or not _VALID_TABLE_RE.match(name):
        raise ValueError(f"Invalid table name: {name!r}")
    return name.replace("'", "''")

def _validate_field_name(name: str) -> str:
    """Validate and sanitize a field name for use in SQL."""
    if not name or not re.match(r'^[A-Za-z_][A-Za-z0-9_]{0,40}$', name):
        raise ValueError(f"Invalid field name: {name!r}")
    return name


def _normalize_field_list(fields: str) -> str:
    """Normalize a comma-separated field list for SAP RFC calls.

    Removes spaces around commas and trims each field name.
    Example: 'MATNR, ERNAM, MTART' → 'MATNR,ERNAM,MTART'
    This prevents ABAP dynamic SELECT from misinterpreting field names
    when spaces are present after commas.
    """
    if not fields or fields.strip() == '*':
        return '*'
    parts = [f.strip() for f in fields.split(',')]
    parts = [p for p in parts if p]  # remove empty entries
    return ','.join(parts)


def _parse_rowdata(rowdata: str, expected_count: int = None,
                   context: str = '') -> list:
    """Parse a pipe-delimited ROWDATA string from SAP RFC.

    - Splits by '|'
    - Strips trailing spaces from each value (SAP CHAR fields are
      blank-padded to the field length)
    - Validates against expected_count if provided
    - Pads with None or truncates with a warning log on mismatch

    Args:
        rowdata: The ROWDATA string from ET_DATA
        expected_count: Expected number of fields (from ET_FIELDS or MSSQL)
        context: Description for warning messages (e.g. table name)

    Returns:
        List of string values (stripped of trailing spaces)
    """
    values = [v.rstrip() for v in rowdata.split('|')]

    if expected_count is not None and len(values) != expected_count:
        if len(values) < expected_count:
            log.warning(
                f"  {context}: ROWDATA has {len(values)} fields, "
                f"expected {expected_count} — padding with NULL")
            values.extend([None] * (expected_count - len(values)))
        else:
            log.warning(
                f"  {context}: ROWDATA has {len(values)} fields, "
                f"expected {expected_count} — truncating")
            values = values[:expected_count]

    return values


def _get_sap_field_names(et_fields: list) -> list:
    """Extract field names from SAP ET_FIELDS metadata.

    ET_FIELDS is a table of ZSQL_FIELD structures with 'FIELDNAME' key.
    Returns ordered list of field names as SAP sees them.
    """
    return [f['FIELDNAME'] for f in et_fields]


# ============================================================================
# Helpers — shared window range calculation
# ============================================================================

def _calculate_window_range(window: str) -> Tuple[str, str]:
    """
    Calculate the date range covering the current AND previous period.
    Returns (from_date, to_date) as YYYYMMDD strings.
    For 'all': returns ('', '') meaning no date filter — full table load.
    to_date is always today (inclusive).
    from_date is the start of the PREVIOUS period.
    """
    today = date.today()
    
    if window == 'all':
        return '', ''
    
    if window == 'day':
        prev = today - timedelta(days=1)
        return prev.strftime('%Y%m%d'), today.strftime('%Y%m%d')
    elif window == 'week':
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
        prev = today - timedelta(days=1)
        return prev.strftime('%Y%m%d'), today.strftime('%Y%m%d')


# ============================================================================
# SAP Connection
# ============================================================================

class SapConnection:
    """Wraps sap_rfc Connection with error handling."""

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

    def rollback(self):
        self.conn.rollback()

    def close(self):
        if self.conn:
            self.conn.close()
            log.info("SQL Server connection closed")


# ============================================================================
# Schema Manager — create tables + indexes from SAP metadata
# ============================================================================

class SchemaManager:
    """Reads table/index metadata from SAP and creates matching tables in MSSQL."""

    def __init__(self, sap: SapConnection, sql: SqlServerConnection):
        self.sap = sap
        self.sql = sql

    def get_table_fields(self, table: str) -> list:
        """Read field definitions from SAP DD03L via Z_EXECUTE_SQL."""
        # Note: Z_EXECUTE_SQL doesn't support parameterized queries via RFC.
        # Sanitize table name to prevent injection.
        safe_table = _validate_table_name(table)
        query = (f"SELECT FIELDNAME, INTTYPE, INTLEN, DECIMALS, KEYFLAG "
                 f"FROM DD03L WHERE TABNAME = '{safe_table}' "
                 f"AND FIELDNAME NOT LIKE '.%' "
                 f"ORDER BY POSITION")
        result = self.sap.call('Z_EXECUTE_SQL',
                               IV_SQL=query,
                               IV_MAX_ROWS=500)
        if result.get('EV_ERROR'):
            log.error(f"Cannot read fields for {table}: {result['EV_ERROR']}")
            return []

        fields = []
        for row in result.get('ET_DATA', []):
            parts = row['ROWDATA'].split('|')
            if len(parts) >= 5:
                fields.append({
                    'name': parts[0].strip(),
                    'inttype': parts[1].strip(),
                    'length': int(parts[2].strip()) if parts[2].strip().isdigit() else 0,
                    'decimals': int(parts[3].strip()) if parts[3].strip().isdigit() else 0,
                    'keyflag': parts[4].strip().upper() == 'X',
                })
        return fields

    def get_table_indexes(self, table: str, fields: list = None) -> list:
        """Read index definitions from SAP DD12L + DD17S via Z_EXECUTE_SQL."""
        # Sanitize table name
        safe_table = _validate_table_name(table)
        # Get index headers
        query = (f"SELECT INDEXNAME, DBINDEX, UNIQUEFLAG "
                 f"FROM DD12L WHERE TABNAME = '{safe_table}' "
                 f"AND AS4LOCAL = 'A' "
                 f"ORDER BY POSITION")
        result = self.sap.call('Z_EXECUTE_SQL',
                               IV_SQL=query,
                               IV_MAX_ROWS=100)
        if result.get('EV_ERROR'):
            log.warning(f"Cannot read indexes for {table}: {result['EV_ERROR']}")
            return []

        indexes = []
        for row in result.get('ET_DATA', []):
            parts = row['ROWDATA'].split('|')
            if len(parts) >= 3:
                idx_name = parts[0].strip()
                db_index = parts[1].strip()
                unique = parts[2].strip().upper() in ('X', '1', 'U')
                indexes.append({
                    'name': idx_name,
                    'db_name': db_index,
                    'unique': unique,
                    'fields': []
                })

        # Get index fields for each index
        for idx in indexes:
            safe_idx_name = _validate_field_name(idx['name'])
            query = (f"SELECT FIELDNAME, ASCDESC "
                     f"FROM DD17S WHERE TABNAME = '{safe_table}' "
                     f"AND INDEXNAME = '{safe_idx_name}' "
                     f"ORDER BY POSITION")
            result = self.sap.call('Z_EXECUTE_SQL',
                                   IV_SQL=query,
                                   IV_MAX_ROWS=50)
            if result.get('EV_ERROR'):
                continue
            for row in result.get('ET_DATA', []):
                parts = row['ROWDATA'].split('|')
                if len(parts) >= 2:
                    idx['fields'].append({
                        'fieldname': _validate_field_name(parts[0].strip()),
                        'order': 'DESC' if (parts[1].strip().upper() if len(parts) > 1 else 'A') == 'D' else 'ASC'
                    })

        # Also get primary key from DD03L
        if fields is None:
            fields = self.get_table_fields(table)
        pk_fields = [f['name'] for f in fields if f['keyflag']]
        if pk_fields:
            indexes.insert(0, {
                'name': 'PRIMARY_KEY',
                'db_name': '__PK__',
                'unique': True,
                'fields': [{'fieldname': f, 'order': 'ASC'} for f in pk_fields],
                'is_primary': True
            })

        # Mark non-PK indexes
        for idx in indexes:
            if 'is_primary' not in idx:
                idx['is_primary'] = False

        return indexes

    def _sap_type_to_mssql(self, inttype: str, length: int, decimals: int) -> str:
        """Convert SAP internal type to MSSQL column type."""
        t = inttype.upper()
        if t in ('C', 'CHAR'):
            return f'NVARCHAR({max(length, 1)})'
        elif t in ('S', 'STRING'):
            return 'NVARCHAR(MAX)'
        elif t in ('I', 'INT4', 'INT'):
            return 'INT'
        elif t in ('S2', 'INT2'):
            return 'SMALLINT'
        elif t in ('B', 'INT1'):
            return 'TINYINT'
        elif t in ('N', 'NUMC'):
            return f'NVARCHAR({max(length, 1)})'
        elif t in ('P', 'PACK'):
            # SAP PACK: INTLEN is bytes, each byte = 2 digits minus 1 sign nibble
            prec = max(length * 2 - 1, 1)
            scale = min(max(decimals, 0), prec)
            return f'DECIMAL({prec}, {scale})'
        elif t in ('F', 'FLTP'):
            return 'FLOAT(53)'
        elif t in ('D', 'DATS'):
            return 'DATE'
        elif t in ('T', 'TIMS'):
            return 'TIME(0)'
        elif t in ('X', 'RAW'):
            return f'VARBINARY({max(length, 1)})'
        elif t in ('Y', 'LRAW'):
            return 'VARBINARY(MAX)'
        else:
            return f'NVARCHAR({max(length, 1)})'

    def create_table(self, table: str, target_table: str = None,
                     drop_if_exists: bool = False) -> bool:
        """Create a table in MSSQL matching the SAP source table structure."""
        target = target_table or table
        fields = self.get_table_fields(table)

        if not fields:
            log.error(f"Cannot create {target}: no fields found for {table}")
            return False

        # Build CREATE TABLE statement
        # Validate target table name early — used in constraint name and DDL
        safe_target = _validate_table_name(target)

        col_defs = []
        pk_cols = []
        for f in fields:
            mssql_type = self._sap_type_to_mssql(f['inttype'], f['length'], f['decimals'])
            col_name = _validate_field_name(f['name'])
            col_defs.append(f"  [{col_name}] {mssql_type}")
            if f['keyflag']:
                pk_cols.append(f"[{col_name}]")

        if pk_cols:
            col_defs.append(f"  CONSTRAINT [PK_{safe_target}] PRIMARY KEY ({', '.join(pk_cols)})")

        # Drop if exists
        if drop_if_exists:
            log.info(f"  Dropping dbo.[{safe_target}] if exists")
            self.sql.execute(f"IF OBJECT_ID('dbo.[{safe_target}]', 'U') IS NOT NULL DROP TABLE dbo.[{safe_target}]")
            self.sql.commit()

        create_sql = f"CREATE TABLE dbo.[{safe_target}] (\n" + ",\n".join(col_defs) + "\n)"

        try:
            log.info(f"  Creating table dbo.{target} with {len(fields)} columns")
            self.sql.execute(create_sql)
            self.sql.commit()
            log.info(f"  Table dbo.{target} created")
        except Exception as e:
            log.error(f"  Cannot create table: {e}")
            return False

        # Create indexes
        return self.create_indexes(table, target, fields=fields)

    def create_indexes(self, table: str, target_table: str = None,
                       fields: list = None) -> bool:
        """Create indexes on MSSQL table matching SAP indexes."""
        target = target_table or table
        safe_target = _validate_table_name(target)
        indexes = self.get_table_indexes(table, fields=fields)

        if not indexes:
            log.info(f"  No indexes found for {table}")
            return True

        created = 0
        for idx in indexes:
            if idx.get('is_primary'):
                # Primary key already created with table
                continue

            if not idx['fields']:
                continue

            # Build index name — sanitize SAP index name for MSSQL
            idx_name = f"IX_{safe_target}_{idx['name']}"
            # MSSQL index name max 128 chars
            idx_name = idx_name[:128]

            col_list = ', '.join(
                f"[{f['fieldname']}] {f['order']}" for f in idx['fields']
            )

            unique_str = "UNIQUE " if idx['unique'] else ""

            create_idx_sql = (
                f"CREATE {unique_str}NONCLUSTERED INDEX [{idx_name}] "
                f"ON dbo.[{safe_target}] ({col_list})"
            )

            try:
                log.info(f"  Creating index {idx_name} on {target} ({col_list})")
                self.sql.execute(create_idx_sql)
                self.sql.commit()
                created += 1
            except Exception as e:
                log.warning(f"  Cannot create index {idx_name}: {e}")

        log.info(f"  Created {created} indexes for dbo.{target}")
        return True

    def sync_schema(self, table: str, target_table: str = None,
                    drop_if_exists: bool = False) -> bool:
        """Create table + indexes in MSSQL from SAP metadata."""
        target = target_table or table
        log.info(f"SCHEMA SYNC: {table} → dbo.{target}")
        return self.create_table(table, target, drop_if_exists)


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
        # Use IF EXISTS pattern instead of MERGE for broader compatibility
        cursor = self.sql.execute(
            "SELECT COUNT(*) FROM CDC_STATE WHERE table_name = ?", (table,)
        )
        row = cursor.fetchone()
        if row and row[0] > 0:
            self.sql.execute(
                "UPDATE CDC_STATE SET last_seq = ?, last_sync = GETDATE(), mode = ? WHERE table_name = ?",
                (seq, mode, table)
            )
        else:
            self.sql.execute(
                "INSERT INTO CDC_STATE (table_name, last_seq, last_sync, mode) VALUES (?, ?, GETDATE(), ?)",
                (table, seq, mode)
            )
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
        safe_table = _validate_table_name(table)
        cursor = self.sql.execute(f"SELECT TOP 0 * FROM dbo.[{safe_table}]")
        col_names = [desc[0] for desc in cursor.description]

        # Case-insensitive matching: normalize both to uppercase for
        # membership/index tests, but keep original col_names for SQL f-strings.
        col_names_upper = [c.upper() for c in col_names]
        key_fields_upper = [k.upper() for k in key_fields]
        # Map each key field (original case from config) to its actual DB column name
        key_col_map = {k: col_names[col_names_upper.index(ku)]
                       for k, ku in zip(key_fields, key_fields_upper)}

        # Validate that all key_fields exist in the target table columns
        missing_keys = [k for k, ku in zip(key_fields, key_fields_upper)
                        if ku not in col_names_upper]
        if missing_keys:
            log.error(f"  Key fields not found in target table: {missing_keys}")
            return 0

        inserts = []
        updates = []
        deletes = []

        for rowdata in rows:
            # Use _parse_rowdata for trailing space trimming (SAP CHAR fields
            # are blank-padded). expected_count=None since CDC rows have
            # OPERATION prefix + variable field count.
            parts = _parse_rowdata(rowdata, context=table)
            operation = parts[0]
            values = parts[1:]

            if operation == 'D':
                # DELETE — store full values; key extraction by position later
                deletes.append(values)
            elif operation in ('I', 'U'):
                # INSERT or UPDATE (UPSERT via MERGE)
                if len(values) >= len(col_names):
                    vals = values[:len(col_names)]
                else:
                    vals = values + [None] * (len(col_names) - len(values))

                if operation == 'I':
                    inserts.append(vals)
                else:  # U
                    updates.append(vals)

        # Batch INSERTs — use MERGE for UPSERT semantics (handles both I and U)
        if inserts:
            col_list = ','.join(f"[{c}]" for c in col_names)
            placeholders = ','.join(['?'] * len(col_names))
            # Use MERGE for UPSERT — works for both INSERT (new row) and
            # UPDATE (row already exists from initial load)
            key_col_list = ' AND '.join(
                f"target.[{key_col_map[k]}] = source.[{key_col_map[k]}]"
                for k in key_fields)
            non_key_cols = [c for c, cu in zip(col_names, col_names_upper)
                            if cu not in key_fields_upper]
            if non_key_cols:
                set_clause = ','.join(f"target.[{c}] = source.[{c}]" for c in non_key_cols)
                merge_sql = (
                    f"MERGE dbo.[{safe_table}] AS target "
                    f"USING (VALUES ({placeholders})) AS source ({col_list}) "
                    f"ON {key_col_list} "
                    f"WHEN MATCHED THEN UPDATE SET {set_clause} "
                    f"WHEN NOT MATCHED THEN INSERT ({col_list}) VALUES ({placeholders});"
                )
                cursor = self.sql.conn.cursor()
                for vals in inserts:
                    cursor.execute(merge_sql, vals + vals)
                self.sql.conn.commit()
            else:
                # All columns are key fields — MERGE with WHEN NOT MATCHED only
                # (plain INSERT would fail on PK violation for existing rows)
                merge_sql = (
                    f"MERGE dbo.[{safe_table}] AS target "
                    f"USING (VALUES ({placeholders})) AS source ({col_list}) "
                    f"ON {key_col_list} "
                    f"WHEN NOT MATCHED THEN INSERT ({col_list}) VALUES ({placeholders});"
                )
                cursor = self.sql.conn.cursor()
                for vals in inserts:
                    cursor.execute(merge_sql, vals + vals)
                self.sql.conn.commit()

        # Batch UPDATEs (executemany with per-row WHERE)
        if updates:
            non_key_update = [c for c, cu in zip(col_names, col_names_upper)
                              if cu not in key_fields_upper]
            set_clause = ','.join(f"[{c}] = ?" for c in non_key_update)
            where = ' AND '.join(f"[{key_col_map[k]}] = ?" for k in key_fields)
            if not set_clause:
                log.warning(f"  UPDATE skipped — no non-key columns to set")
            else:
                for vals in updates:
                    update_vals = [v for c, v in zip(col_names, vals)
                                   if c in non_key_update]
                    update_vals += [vals[col_names_upper.index(ku)]
                                    for ku in key_fields_upper]
                    self.sql.execute(
                        f"UPDATE dbo.[{safe_table}] SET {set_clause} WHERE {where}",
                        update_vals
                    )
                self.sql.commit()

        # Batch DELETEs
        if deletes:
            for key_vals in deletes:
                # Extract key values by position (key fields may not be first columns)
                if len(key_vals) == len(key_fields):
                    # CDC log sent only key values — use as-is
                    pass
                else:
                    # Full row — extract key field values by column position
                    # Pad key_vals to match col_names length to prevent IndexError
                    if len(key_vals) < len(col_names):
                        key_vals = key_vals + [None] * (len(col_names) - len(key_vals))
                    key_vals = [key_vals[col_names_upper.index(ku)]
                                for ku in key_fields_upper]
                where = ' AND '.join(f"[{key_col_map[k]}] = ?" for k in key_fields)
                self.sql.execute(f"DELETE FROM dbo.[{safe_table}] WHERE {where}", key_vals)
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
        key_field_list = [_validate_field_name(k.strip()) for k in key_fields.split(',')]

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
                if next_seq > last_seq:
                    max_seq = next_seq - 1
            prev_last_seq = last_seq
            last_seq = next_seq
            if next_seq <= prev_last_seq and rows:
                log.warning(f"  {table}: seq not advancing (next_seq={next_seq}, last_seq={prev_last_seq}) — stopping to prevent infinite loop")
                break
            if not has_more:
                break
            # Guard: if no rows but has_more is true, break to prevent infinite loop
            if not rows and has_more:
                log.warning(f"  {table}: has_more=true but no rows — breaking to prevent loop")
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
        """Delegate to shared window range calculator."""
        return _calculate_window_range(window)

    def sync_table(self, table: str, delta_field: str, window: str = 'month',
                   chunk_size: int = 10000):
        """Sync a table using timeframe delta (trigger-free).
        
        Always loads current + previous period to prevent data loss at boundaries.
        Deletes the same range in the target table before inserting.
        """
        safe_table = _validate_table_name(table)
        safe_delta = _validate_field_name(delta_field)
        date_from, date_to = self._get_window_range(window)
        
        if window == 'all' or not date_from:
            # Full table replace — TRUNCATE + load all
            log.info(f"TIMEFRAME sync: {table} (window=all → full table replace)")
            self.sql.execute(f"TRUNCATE TABLE dbo.[{safe_table}]")
            self.sql.commit()
            where_clause = ""  # no WHERE filter
        else:
            log.info(f"TIMEFRAME sync: {table} where {delta_field} >= '{date_from}' "
                     f"(window={window}, current+previous period)")

            # Step 1: Delete current+previous period from target
            # Convert YYYYMMDD to YYYY-MM-DD for MSSQL DATE columns
            mssql_date_from = f"{date_from[:4]}-{date_from[4:6]}-{date_from[6:8]}"
            log.info(f"  Deleting rows from dbo.[{safe_table}] where {safe_delta} >= '{mssql_date_from}'")
            self.sql.execute(
                f"DELETE FROM dbo.[{safe_table}] WHERE [{safe_delta}] >= ?",
                (mssql_date_from,)
            )

            # Step 2: Read from SAP via Z_READ_TABLE with WHERE clause
            # SAP expects YYYYMMDD format (not YYYY-MM-DD)
            where_clause = f"{safe_delta} >= '{date_from}'"
        total_rows = 0
        skip = 0

        # Column metadata: prefer SAP ET_FIELDS (authoritative) over MSSQL
        sap_field_names = None
        col_names = None
        expected_cols = None

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

            # Get column names: prefer SAP ET_FIELDS metadata (authoritative)
            if col_names is None:
                et_fields = result.get('ET_FIELDS', [])
                if et_fields:
                    sap_field_names = _get_sap_field_names(et_fields)
                    log.info(f"  SAP ET_FIELDS returned {len(sap_field_names)} fields")
                # Fallback: get column names from target MSSQL table
                cursor = self.sql.execute(
                    f"SELECT TOP 0 * FROM dbo.[{safe_table}]")
                col_names = [desc[0] for desc in cursor.description]

                # Use SAP field count for parsing (authoritative from Z_READ_TABLE)
                if sap_field_names:
                    if len(sap_field_names) != len(col_names):
                        log.warning(
                            f"  {table}: SAP ET_FIELDS has {len(sap_field_names)} fields "
                            f"but MSSQL table has {len(col_names)} columns -- "
                            f"using SAP field count for ROWDATA parsing")
                    expected_cols = len(sap_field_names)
                else:
                    expected_cols = len(col_names)

            # Build INSERT batch using _parse_rowdata for trailing space trimming
            placeholders = ','.join(['?'] * len(col_names))
            col_list = ','.join(f"[{c}]" for c in col_names)
            insert_sql = f"INSERT INTO dbo.[{safe_table}] ({col_list}) VALUES ({placeholders})"

            batch = []
            for row in data_rows:
                values = _parse_rowdata(row['ROWDATA'], expected_cols, table)
                # Map SAP field count to MSSQL column count
                if len(values) >= len(col_names):
                    vals = values[:len(col_names)]
                else:
                    vals = values + [None] * (len(col_names) - len(values))
                batch.append(vals)

            self.sql.executemany(insert_sql, batch)
            self.sql.commit()

            total_rows += len(data_rows)
            skip += chunk_size
            log.info(f"  {table}: loaded {len(data_rows)} rows (total: {total_rows})")

            if result.get('EV_HAS_MORE') != 'X':
                break

        log.info(f"TIMEFRAME sync complete: {table} -- {total_rows} rows loaded")
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
        safe_table = _validate_table_name(table)
        log.info(f"FULL LOAD: {table}")

        # Step 1: Truncate target table
        log.info(f"  Truncating dbo.[{safe_table}]")
        self.sql.execute(f"TRUNCATE TABLE dbo.[{safe_table}]")
        self.sql.commit()

        # Step 2: Read all data via Z_READ_TABLE
        total_rows = 0
        skip = 0
        col_names = None
        sap_field_names = None
        expected_cols = None

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
                et_fields = result.get('ET_FIELDS', [])
                if et_fields:
                    sap_field_names = _get_sap_field_names(et_fields)
                    log.info(f"  SAP ET_FIELDS returned {len(sap_field_names)} fields")
                cursor = self.sql.execute(
                    f"SELECT TOP 0 * FROM dbo.[{safe_table}]")
                col_names = [desc[0] for desc in cursor.description]

                # Use SAP field count for parsing (authoritative from Z_READ_TABLE)
                if sap_field_names:
                    if len(sap_field_names) != len(col_names):
                        log.warning(
                            f"  {table}: SAP ET_FIELDS has {len(sap_field_names)} fields "
                            f"but MSSQL table has {len(col_names)} columns -- "
                            f"using SAP field count for ROWDATA parsing")
                    expected_cols = len(sap_field_names)
                else:
                    expected_cols = len(col_names)

            placeholders = ','.join(['?'] * len(col_names))
            col_list = ','.join(f"[{c}]" for c in col_names)
            insert_sql = f"INSERT INTO dbo.[{safe_table}] ({col_list}) VALUES ({placeholders})"

            batch = []
            for row in data_rows:
                values = _parse_rowdata(row['ROWDATA'], expected_cols, table)
                # Map SAP field count to MSSQL column count
                if len(values) >= len(col_names):
                    vals = values[:len(col_names)]
                else:
                    vals = values + [None] * (len(col_names) - len(values))
                batch.append(vals)

            self.sql.executemany(insert_sql, batch)
            self.sql.commit()

            total_rows += len(data_rows)
            skip += chunk_size

            if total_rows % 100000 == 0:
                log.info(f"  {table}: loaded {total_rows} rows...")

            if result.get('EV_HAS_MORE') != 'X':
                break

        log.info(f"FULL LOAD complete: {table} -- {total_rows} rows")
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
        """Delegate to shared window range calculator."""
        return _calculate_window_range(window)

    def _download_file(self, remote_path: str, local_path: str) -> bool:
        """Download file from SAP server — supports SCP, SMB, and local methods."""

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
        import csv

        # Read CSV header to get column names
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.reader(f, delimiter='|')
            header = next(reader)

        col_names = [h.strip() for h in header]
        # Validate column names from CSV header
        for c in col_names:
            _validate_field_name(c)
        col_list = ','.join(f"[{c}]" for c in col_names)

        # Replace mode: delete target rows before insert
        safe_target_tbl = _validate_table_name(target_table)

        if replace_mode == 'replace_all':
            log.info(f"  TRUNCATE dbo.[{safe_target_tbl}]")
            self.sql.execute(f"TRUNCATE TABLE dbo.[{safe_target_tbl}]")
            self.sql.commit()
        elif replace_mode == 'replace_window' and date_field and date_from:
            safe_date_field = _validate_field_name(date_field)
            # date_from is YYYYMMDD from _get_window_range — convert to YYYY-MM-DD for MSSQL
            mssql_date_from = f"{date_from[:4]}-{date_from[4:6]}-{date_from[6:8]}"
            if date_to:
                mssql_date_to = f"{date_to[:4]}-{date_to[4:6]}-{date_to[6:8]}"
                log.info(f"  DELETE FROM dbo.[{safe_target_tbl}] WHERE [{safe_date_field}] >= ? AND [{safe_date_field}] <= ?")
                self.sql.execute(
                    f"DELETE FROM dbo.[{safe_target_tbl}] WHERE [{safe_date_field}] >= ? AND [{safe_date_field}] <= ?",
                    (mssql_date_from, mssql_date_to)
                )
            else:
                log.info(f"  DELETE FROM dbo.[{safe_target_tbl}] WHERE [{safe_date_field}] >= ?")
                self.sql.execute(
                    f"DELETE FROM dbo.[{safe_target_tbl}] WHERE [{safe_date_field}] >= ?",
                    (mssql_date_from,)
                )
            self.sql.commit()

        # Use BULK INSERT via pyodbc (fastest method)
        # Pass the absolute path directly — BULK INSERT accepts single-quoted paths
        csv_abs = os.path.abspath(csv_path).replace("'", "''")

        bulk_sql = f"""
            BULK INSERT dbo.[{safe_target_tbl}]
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
            log.info(f"  BULK INSERT into dbo.[{safe_target_tbl}]")
            cursor = self.sql.execute(bulk_sql)
            self.sql.commit()

            # Get row count from the BULK INSERT cursor directly
            # (using @@ROWCOUNT in a separate query would return 1 — the count of SELECT itself)
            row_count = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
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

        safe_target_tbl = _validate_table_name(target_table)
        placeholders = ','.join(['?'] * len(col_names))
        col_list = ','.join(f"[{c}]" for c in col_names)
        insert_sql = f"INSERT INTO dbo.[{safe_target_tbl}] ({col_list}) VALUES ({placeholders})"

        batch = []
        total = 0
        batch_size = 5000

        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.reader(f, delimiter='|')
            next(reader)  # skip header

            for row in reader:
                # Pad row if needed
                while len(row) < len(col_names):
                    row.append(None)
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
        # Normalize field list: remove spaces after commas (prevents ABAP
        # dynamic SELECT from misinterpreting field names)
        fields = _normalize_field_list(fields)
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
                try:
                    self.sap.call('Z_DELETE_FILE', IV_FILE_PATH=remote_file)
                except Exception as e:
                    log.warning(f"  Remote cleanup failed (empty file): {e}")
            return True

        # Step 2: Download file from SAP server
        import tempfile
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
            try:
                self.sap.call('Z_DELETE_FILE', IV_FILE_PATH=remote_file)
            except Exception as e:
                log.warning(f"  Remote cleanup failed (data already loaded): {e}")

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
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def import_tables(config_path: str, config: dict, file_path: str):
    """Import table names from a file into config as inactive entries.

    Reads a file with one table name per line. Deduplicates against
    existing tables in config. New tables are added with mode='full',
    active=false, chunk_size=10000, fields='*'.
    """
    if not os.path.exists(file_path):
        log.error(f"File not found: {file_path}")
        sys.exit(1)

    # Read table names from file
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    # Parse and validate table names
    new_names = []
    for line in lines:
        name = line.strip()
        if not name or name.startswith('#'):
            continue
        try:
            _validate_table_name(name)
        except ValueError as e:
            log.warning(f"  Skipping invalid table name: {name!r} ({e})")
            continue
        new_names.append(name.upper())

    # Deduplicate within the file itself
    seen = set()
    unique_names = []
    for n in new_names:
        if n not in seen:
            seen.add(n)
            unique_names.append(n)

    # Get existing table names (case-insensitive)
    existing_names = {t.get('name', '').upper() for t in config.get('tables', [])}

    # Find new tables
    added = []
    for name in unique_names:
        if name not in existing_names:
            added.append(name)

    # Add new tables to config
    for name in added:
        config.setdefault('tables', []).append({
            'name': name,
            'mode': 'full',
            'active': False,
            'chunk_size': 10000,
            'fields': '*'
        })

    # Write updated config back
    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2)

    already_existed = len(unique_names) - len(added)
    log.info(f"Imported {len(added)} new tables from {file_path}, "
             f"{already_existed} already existed")

    if added:
        log.info("  New tables (inactive, review and activate):")
        for name in added:
            log.info(f"    {name}")
    else:
        log.info("  No new tables to add")


def run_table(table_cfg: dict, sap: SapConnection, sql: SqlServerConnection,
              state: StateManager, config: dict = None,
              mode_override: str = None, window_override: str = None):
    """Run sync for a single table based on its config."""

    table = table_cfg['name']

    # Skip inactive tables
    if not table_cfg.get('active', True):
        log.info(f"  {table}: skipped (inactive)")
        return True

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
            # Use date_field from config, fall back to delta_field
            effective_date_field = date_field or delta_field
            return replicator.sync_table(
                table=table,
                target_table=target_table,
                date_field=effective_date_field,
                window=window,
                fields=fields,
                max_rows=max_rows,
                file_path=file_path,
                replace_mode=replace_mode
            )

        else:
            log.error(f"  {table}: unknown mode '{mode}'")
            return False

    except Exception as e:
        log.error(f"  {table}: sync failed — {e}")
        sql.rollback()
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
    parser.add_argument('--sync-schema', metavar='TABLE',
                        help='Create table + indexes in MSSQL from SAP metadata')
    parser.add_argument('--sync-schema-all', action='store_true',
                        help='Create tables + indexes for all configured tables')
    parser.add_argument('--import-tables', metavar='FILE',
                        help='Import table names from a file (one per line) into config as inactive entries')
    args = parser.parse_args()

    config = load_config(args.config)

    # --import-tables: read table names from file, add new ones as inactive
    if args.import_tables:
        import_tables(args.config, config, args.import_tables)
        return

    # Connect
    sap = SapConnection(config['sap'])
    sql = SqlServerConnection(config['sql_server'])

    try:
        sap.connect()
        sql.connect()
        state = StateManager(sql)

        # Special commands
        if args.remove_cdc:
            cdc = CdcReplicator(sap, sql, state)
            cdc.remove_cdc(args.remove_cdc)
            return

        if args.sync_schema:
            schema = SchemaManager(sap, sql)
            schema.sync_schema(args.sync_schema, drop_if_exists=True)
            return

        if args.sync_schema_all:
            schema = SchemaManager(sap, sql)
            for t in config['tables']:
                target = t.get('target_table', t['name'])
                schema.sync_schema(t['name'], target, drop_if_exists=True)
            return

        if args.init_only:
            tables = config['tables']
            if args.table:
                tables = [t for t in tables if t['name'] == args.table]
            else:
                tables = [t for t in tables if t.get('active', True)]
            cdc = CdcReplicator(sap, sql, state)
            for t in tables:
                if t.get('mode') == 'cdc' and t.get('key_fields'):
                    cdc.init_table(t['name'], t['key_fields'])
            return

        # Normal sync
        tables = config['tables']
        if args.table:
            tables = [t for t in tables if t['name'] == args.table]

        # Filter out inactive tables (unless a specific table was requested)
        if not args.table:
            active_tables = [t for t in tables if t.get('active', True)]
            skipped = len(tables) - len(active_tables)
            if skipped > 0:
                log.info(f"Skipping {skipped} inactive table(s)")
            tables = active_tables

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
        try:
            sap.close()
        except Exception:
            pass
        try:
            sql.close()
        except Exception:
            pass


if __name__ == '__main__':
    main()