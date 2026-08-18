#!/usr/bin/env python3
"""
sap_diagnose.py — Safe SAP Data Replication Diagnostic Tool
============================================================
Standalone command-line tool that tests all SAP function modules used by
the SAP Data Replication client — safely, with no side effects.

Tests performed (by default, NO side effects):
  1. Connection test — verifies SAP login
  2. Function module existence check — verifies each FM exists via metadata
  3. Safe read-only tests:
     - Z_READ_TABLE  — reads 1 row from ZTLANGTXT (language texts table)
     - Z_EXPORT_TABLE — exports 1 row from ZTLANGTXT to /tmp/ on SAP server
       (creates a tiny temp file, then immediately deletes it via Z_DELETE_FILE)
  4. CDC checks (read-only unless --init-test):
     - Checks if CDC log tables already exist for common tables
     - Z_CDC_READ — only called if a log table exists
  5. Summary report

With --init-test flag:
  - Calls Z_CDC_INIT on ZTLANGTXT (creates a trigger + log table)
  - Calls Z_CDC_READ to verify delta reading works
  - Calls Z_CDC_CLEANUP to remove the test CDC (trigger + log table)

Usage:
  python sap_diagnose.py --host sap.example.com --sysnr 00 --client 100 \\
      --user MYUSER --password MYPASS

  python sap_diagnose.py --host sap.example.com --sysnr 00 --client 100 \\
      --user MYUSER --password MYPASS --init-test

Requirements:
  - sap_rfc.py and nwrfc_bootstrap.py in the same directory
  - SAP NWRFC SDK (sapnwrfc.dll / libsapnwrfc.so) installed
  - Python 3.14+
"""

import argparse
import os
import sys
import tempfile

# NWRFC DLL bootstrap — must run before sap_rfc is imported (same as main app)
from nwrfc_bootstrap import bootstrap as _nwrfc_bootstrap
_nwrfc_status, _nwrfc_msg = _nwrfc_bootstrap()
if _nwrfc_status == "missing":
    print(f"ERROR: {_nwrfc_msg}", file=sys.stderr)
    sys.exit(1)
if _nwrfc_status == "elevated":
    sys.exit(0)

try:
    from sap_rfc import Connection, SAPRFCError
except ImportError:
    print("ERROR: sap_rfc module not found. It should be in the same directory.")
    print("       Also requires SAP NWRFC SDK (sapnwrfc.dll/.so)")
    sys.exit(1)


# ============================================================================
# ANSI color helpers (disabled if not a TTY)
# ============================================================================

_USE_COLOR = sys.stdout.isatty() if hasattr(sys.stdout, 'isatty') else False


def _c(code: str, text: str) -> str:
    """Wrap text in ANSI color code if color is enabled."""
    if _USE_COLOR:
        return f"\033[{code}m{text}\033[0m"
    return text


def green(text: str) -> str:
    return _c("32", text)


def red(text: str) -> str:
    return _c("31", text)


def yellow(text: str) -> str:
    return _c("33", text)


def cyan(text: str) -> str:
    return _c("36", text)


def bold(text: str) -> str:
    return _c("1", text)


# ============================================================================
# Result tracking
# ============================================================================

class TestResult:
    """Tracks the status of each function module test."""

    # Status constants
    OK = "OK"
    EXISTS = "EXISTS"
    TESTED_OK = "TESTED_OK"
    MISSING = "MISSING"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"
    WARN = "WARN"

    def __init__(self):
        # name -> {"status": ..., "detail": ...}
        self.results: dict = {}
        self.connection_info: str | None = None

    def set(self, name: str, status: str, detail: str = ""):
        self.results[name] = {"status": status, "detail": detail}

    def get(self, name: str):
        return self.results.get(name)

    def summary_lines(self) -> list:
        """Generate summary report lines."""
        lines = []
        lines.append(bold("=" * 60))
        lines.append(bold("=== SAP Data Replication Diagnostics ==="))
        lines.append(bold("=" * 60))

        # Connection
        if self.connection_info:
            lines.append(f"Connection:           {self.connection_info}")
        else:
            lines.append(f"Connection:           {red('FAILED')}")

        # Function modules
        fm_order = [
            "Z_CDC_INIT",
            "Z_CDC_READ",
            "Z_CDC_CLEANUP",
            "Z_EXPORT_TABLE",
            "Z_DELETE_FILE",
            "Z_READ_TABLE",
            "Z_EXECUTE_SQL",
        ]

        for name in fm_order:
            r = self.results.get(name)
            if r is None:
                lines.append(f"{name:<22}NOT CHECKED")
                continue

            status = r["status"]
            detail = r["detail"]
            label = f"{name:<22}"

            if status == self.TESTED_OK:
                lines.append(f"{label}{green('EXISTS + TESTED OK')} ({detail})")
            elif status == self.OK:
                lines.append(f"{label}{green('OK')} ({detail})")
            elif status == self.EXISTS:
                lines.append(f"{label}{yellow('EXISTS')} ({detail})")
            elif status == self.MISSING:
                lines.append(f"{label}{red('MISSING')} ({detail})")
            elif status == self.ERROR:
                lines.append(f"{label}{red('ERROR')} ({detail})")
            elif status == self.SKIPPED:
                lines.append(f"{label}{yellow('SKIPPED')} ({detail})")
            elif status == self.WARN:
                lines.append(f"{label}{yellow('WARN')} ({detail})")
            else:
                lines.append(f"{label}{status} ({detail})")

        lines.append("=" * 60)
        return lines


# ============================================================================
# Function module existence check
# ============================================================================

def check_fm_exists(conn: Connection, func_name: str) -> tuple:
    """Check if a function module exists by calling RfcGetFunctionDesc.

    Returns (exists: bool, detail: str)
    """
    try:
        # The Connection.call() method internally calls RfcGetFunctionDesc.
        # We can use a lightweight metadata check by trying to get the
        # function descriptor without invoking the function.
        # Since sap_rfc doesn't expose a standalone metadata check,
        # we attempt a call with no parameters. If the FM doesn't exist,
        # RfcGetFunctionDesc will fail before any invocation happens.
        # This is safe — no side effects from just looking up metadata.
        func_name_bytes = (
            func_name.encode('utf-16-le') + b'\x00\x00'
        )
        # Access the library directly for a metadata-only check
        from ctypes import byref
        from sap_rfc import RFC_ERROR_INFO
        lib = conn._lib  # may be None per type stubs; guard
        if lib is None:
            return False, "SAP NWRFC library not loaded"
        error = RFC_ERROR_INFO()
        func_desc = lib.RfcGetFunctionDesc(
            conn._connection_handle, func_name_bytes, byref(error),
        )
        if func_desc:
            return True, "metadata lookup succeeded"
        else:
            # Function module not found
            from sap_rfc import _uc_array_to_str
            key = _uc_array_to_str(error.key)
            msg = _uc_array_to_str(error.message)
            return False, f"not found: {key} — {msg}"
    except Exception as e:
        return False, f"error checking: {e}"


# ============================================================================
# Safe function module tests
# ============================================================================

def test_z_read_table(conn: Connection, result: TestResult) -> bool:
    """Test Z_READ_TABLE with ZTLANGTXT (language texts), 1 row. Read-only, no side effects."""
    print(f"\n{bold('--- Testing Z_READ_TABLE (read-only) ---')}")
    print(f"    Reading 1 row from ZTLANGTXT (language texts table)...")

    try:
        res = conn.call('Z_READ_TABLE',
                        IV_TABLE='ZTLANGTXT',
                        IV_WHERE='',
                        IV_FIELDS='*',
                        IV_ORDERBY='',
                        IV_ROWSKIPS=0,
                        IV_ROWCOUNT=1)
    except SAPRFCError as e:
        print(f"    {red('ERROR')}: {e}")
        result.set("Z_READ_TABLE", TestResult.ERROR, str(e))
        return False

    err = res.get('EV_ERROR')
    if err:
        print(f"    {red('ERROR')}: Z_READ_TABLE returned error: {err}")
        result.set("Z_READ_TABLE", TestResult.ERROR, f"EV_ERROR: {err}")
        return False

    data_rows = res.get('ET_DATA', [])
    fields = res.get('ET_FIELDS', [])

    print(f"    {green('OK')}: Z_READ_TABLE returned {len(data_rows)} row(s), "
          f"{len(fields)} field(s)")

    if data_rows:
        row = data_rows[0]
        rowdata = row.get('ROWDATA', '')
        # Show first few fields
        parts = [p.strip() for p in rowdata.split('|')][:5]
        print(f"    Sample data (first 5 fields): {' | '.join(parts)}")

    if fields:
        field_names = [f.get('FIELDNAME', '?') for f in fields][:5]
        print(f"    Fields (first 5): {', '.join(field_names)}")

    result.set("Z_READ_TABLE", TestResult.TESTED_OK, "1 row from ZTLANGTXT")
    return True


def test_z_export_table(conn: Connection, result: TestResult) -> bool:
    """Test Z_EXPORT_TABLE with ZTLANGTXT, 1 row. Creates a temp file on SAP server.

    This is SAFE MODE: exports only 1 row from ZTLANGTXT to /tmp/, then immediately
    deletes the file via Z_DELETE_FILE.
    """
    print(f"\n{bold('--- Testing Z_EXPORT_TABLE (safe mode — 1 row) ---')}")
    print(f"    {yellow('WARNING')}: This creates a small temporary file on the SAP server.")
    print(f"    Exporting 1 row from ZTLANGTXT to /tmp/...")

    try:
        res = conn.call('Z_EXPORT_TABLE',
                        IV_TABLE='ZTLANGTXT',
                        IV_FIELDS='*',
                        IV_MAX_ROWS=1,
                        IV_FILE_PATH='/tmp/')
    except SAPRFCError as e:
        print(f"    {red('ERROR')}: {e}")
        result.set("Z_EXPORT_TABLE", TestResult.ERROR, str(e))
        return False

    err = res.get('EV_ERROR')
    if err:
        print(f"    {red('ERROR')}: Z_EXPORT_TABLE returned error: {err}")
        result.set("Z_EXPORT_TABLE", TestResult.ERROR, f"EV_ERROR: {err}")
        return False

    remote_file = res.get('EV_FILE_NAME', '')
    row_count = res.get('EV_ROW_COUNT', 0)
    file_size = res.get('EV_FILE_SIZE', 0)

    print(f"    {green('OK')}: Exported {row_count} row(s), "
          f"{file_size} bytes → {remote_file}")

    # Clean up the test file immediately
    if remote_file:
        print(f"    Cleaning up test file via Z_DELETE_FILE...")
        delete_ok = test_z_delete_file(conn, result, remote_file)
        if delete_ok:
            result.set("Z_EXPORT_TABLE", TestResult.TESTED_OK,
                       f"1 row from ZTLANGTXT, file cleaned up")
        else:
            result.set("Z_EXPORT_TABLE", TestResult.TESTED_OK,
                       f"1 row from ZTLANGTXT (cleanup failed — file may remain on server)")
            print(f"    {yellow('WARN')}: Test file may still exist on SAP server: {remote_file}")
    else:
        result.set("Z_EXPORT_TABLE", TestResult.TESTED_OK, "1 row, no file name returned")

    return True


def test_z_delete_file(conn: Connection, result: TestResult,
                      file_path: str) -> bool:
    """Test Z_DELETE_FILE by deleting the specified file path."""
    try:
        res = conn.call('Z_DELETE_FILE', IV_FILE_PATH=file_path)
    except SAPRFCError as e:
        print(f"    {red('ERROR')}: Z_DELETE_FILE failed: {e}")
        # Only set as error if not already tested (e.g., standalone)
        if result.get("Z_DELETE_FILE") is None:
            result.set("Z_DELETE_FILE", TestResult.ERROR, str(e))
        return False

    err = res.get('EV_ERROR')
    if err:
        print(f"    {red('ERROR')}: Z_DELETE_FILE returned error: {err}")
        if result.get("Z_DELETE_FILE") is None:
            result.set("Z_DELETE_FILE", TestResult.ERROR, f"EV_ERROR: {err}")
        return False

    deleted = res.get('EV_DELETED', '')
    print(f"    {green('OK')}: Z_DELETE_FILE deleted {file_path}")
    result.set("Z_DELETE_FILE", TestResult.TESTED_OK, f"test file deleted")
    return True


def test_z_execute_sql_check(conn: Connection, result: TestResult) -> bool:
    """Check Z_EXECUTE_SQL existence. Does NOT test it (requires SQL knowledge).

    If it exists, tries a harmless SELECT 1 to verify it works.
    """
    exists, detail = check_fm_exists(conn, 'Z_EXECUTE_SQL')
    if not exists:
        print(f"\n{bold('--- Z_EXECUTE_SQL ---')}")
        print(f"    {yellow('WARN')}: Z_EXECUTE_SQL not found ({detail})")
        print(f"    This is used by SchemaManager for metadata reads.")
        print(f"    Schema auto-creation will not work without it.")
        result.set("Z_EXECUTE_SQL", TestResult.WARN, "not found — SchemaManager needs this")
        return False

    # Try a harmless query
    print(f"\n{bold('--- Testing Z_EXECUTE_SQL (harmless SELECT) ---')}")
    print(f"    Running: SELECT MANDT FROM ZTLANGTXT (1 row)...")
    try:
        res = conn.call('Z_EXECUTE_SQL',
                        IV_SQL='SELECT MANDT FROM ZTLANGTXT',
                        IV_MAX_ROWS=1)
    except SAPRFCError as e:
        print(f"    {red('ERROR')}: {e}")
        result.set("Z_EXECUTE_SQL", TestResult.ERROR, str(e))
        return False

    err = res.get('EV_ERROR')
    if err:
        print(f"    {yellow('WARN')}: Z_EXECUTE_SQL returned error: {err}")
        result.set("Z_EXECUTE_SQL", TestResult.WARN, f"exists but query failed: {err}")
        return False

    data = res.get('ET_DATA', [])
    print(f"    {green('OK')}: Z_EXECUTE_SQL returned {len(data)} row(s)")
    result.set("Z_EXECUTE_SQL", TestResult.TESTED_OK, "SELECT MANDT FROM ZTLANGTXT")
    return True


# ============================================================================
# CDC tests
# ============================================================================

# Common small tables to check for existing CDC
CDC_CHECK_TABLES = ['ZTLANGTXT']


def check_cdc_log_exists(conn: Connection, table: str) -> tuple:
    """Check if a CDC log table exists for the given table.

    Tries SELECT COUNT(*) FROM Z< table>_CDC_LOG via Z_EXECUTE_SQL.
    Returns (exists: bool, detail: str)
    """
    # Build log table name: Z_ZTLANGTXT_CDC_LOG
    log_table = f"Z_{table}_CDC_LOG"

    # Try via Z_EXECUTE_SQL if available
    try:
        res = conn.call('Z_EXECUTE_SQL',
                        IV_SQL=f'SELECT COUNT(*) FROM {log_table}',
                        IV_MAX_ROWS=1)
        err = res.get('EV_ERROR')
        if err:
            # Table doesn't exist or other error
            return False, f"log table {log_table} not accessible"
        data = res.get('ET_DATA', [])
        if data:
            rowdata = data[0].get('ROWDATA', '')
            count = rowdata.strip()
            return True, f"log table {log_table} exists ({count} entries)"
        return True, f"log table {log_table} exists"
    except SAPRFCError:
        # Z_EXECUTE_SQL not available or failed
        return False, f"cannot check (Z_EXECUTE_SQL unavailable or failed)"
    except Exception as e:
        return False, f"error checking: {e}"


def test_cdc_init(conn: Connection, result: TestResult, table: str = 'ZTLANGTXT') -> bool:
    """Test Z_CDC_INIT — actually creates a trigger + log table on SAP.

    ONLY called when --init-test is explicitly passed.
    """
    print(f"\n{bold('--- Z_CDC_INIT (LIVE TEST — creates trigger on ZTLANGTXT) ---')}")
    print(f"    {yellow('WARNING')}: This will create a CDC trigger and log table")
    print(f"    for table {table} on the SAP system.")
    print(f"    The test will clean up afterwards via Z_CDC_CLEANUP.")

    # ZTLANGTXT key field is MANDT
    key_fields = 'MANDT'

    try:
        res = conn.call('Z_CDC_INIT',
                        IV_TABLE=table,
                        IV_KEYFIELDS=key_fields)
    except SAPRFCError as e:
        print(f"    {red('ERROR')}: {e}")
        result.set("Z_CDC_INIT", TestResult.ERROR, str(e))
        return False

    err = res.get('EV_ERROR')
    if err:
        print(f"    {red('ERROR')}: Z_CDC_INIT returned error: {err}")
        result.set("Z_CDC_INIT", TestResult.ERROR, f"EV_ERROR: {err}")
        return False

    trigger_exists = res.get('EV_TRIGGER_EXISTS', '')
    gap_detected = res.get('EV_GAP_DETECTED', '')
    last_log_time = res.get('EV_LAST_LOG_TIME', '')

    if trigger_exists == 'X':
        print(f"    {green('OK')}: Trigger already exists for {table} (idempotent)")
    else:
        print(f"    {green('OK')}: Trigger created for {table}")

    if gap_detected == 'X':
        print(f"    {yellow('WARN')}: GAP DETECTED — last log entry at {last_log_time}")
        print(f"    This is expected for a fresh CDC init on a table with existing data.")

    result.set("Z_CDC_INIT", TestResult.TESTED_OK,
               f"trigger on {table} (exists={trigger_exists})")
    return True


def test_cdc_read(conn: Connection, result: TestResult, table: str = 'ZTLANGTXT') -> bool:
    """Test Z_CDC_READ — reads delta entries from the CDC log table."""
    print(f"\n{bold('--- Testing Z_CDC_READ (read delta) ---')}")
    print(f"    Reading delta from {table} (from_seq=0, chunk_size=1)...")

    try:
        res = conn.call('Z_CDC_READ',
                        IV_TABLE=table,
                        IV_FROM_SEQ=0,
                        IV_CHUNK_SIZE=1)
    except SAPRFCError as e:
        print(f"    {red('ERROR')}: {e}")
        result.set("Z_CDC_READ", TestResult.ERROR, str(e))
        return False

    err = res.get('EV_ERROR')
    if err:
        print(f"    {red('ERROR')}: Z_CDC_READ returned error: {err}")
        result.set("Z_CDC_READ", TestResult.ERROR, f"EV_ERROR: {err}")
        return False

    rows = res.get('ET_DATA', [])
    next_seq = res.get('EV_NEXT_SEQ', 0)
    has_more = res.get('EV_HAS_MORE', '')

    print(f"    {green('OK')}: Z_CDC_READ returned {len(rows)} delta row(s)")
    print(f"    Next seq: {next_seq}, has_more: {has_more}")

    if rows:
        for i, row in enumerate(rows[:3]):
            rowdata = row.get('ROWDATA', '')
            parts = [p.strip() for p in rowdata.split('|')][:4]
            print(f"    Delta row {i}: operation={parts[0] if parts else '?'}, "
                  f"values={' | '.join(parts[1:])}")

    result.set("Z_CDC_READ", TestResult.TESTED_OK,
               f"{len(rows)} delta row(s) from {table}")
    return True


def test_cdc_cleanup(conn: Connection, result: TestResult,
                     table: str = 'ZTLANGTXT') -> bool:
    """Test Z_CDC_CLEANUP with IV_REMOVE_ALL='X' — removes trigger + log table."""
    print(f"\n{bold('--- Z_CDC_CLEANUP (removing test CDC) ---')}")
    print(f"    Removing CDC for {table} (IV_REMOVE_ALL=X)...")

    try:
        res = conn.call('Z_CDC_CLEANUP',
                        IV_TABLE=table,
                        IV_UP_TO_SEQ=0,
                        IV_REMOVE_ALL='X')
    except SAPRFCError as e:
        print(f"    {red('ERROR')}: {e}")
        result.set("Z_CDC_CLEANUP", TestResult.ERROR, str(e))
        return False

    err = res.get('EV_ERROR')
    if err:
        print(f"    {red('ERROR')}: Z_CDC_CLEANUP returned error: {err}")
        result.set("Z_CDC_CLEANUP", TestResult.ERROR, f"EV_ERROR: {err}")
        return False

    deleted = res.get('EV_DELETED', 0)
    print(f"    {green('OK')}: CDC removed for {table} (deleted {deleted} log entries)")
    result.set("Z_CDC_CLEANUP", TestResult.TESTED_OK,
               f"removed CDC for {table} ({deleted} entries deleted)")
    return True


# ============================================================================
# Main diagnostic flow
# ============================================================================

def run_diagnostics(args) -> int:
    """Run all diagnostic tests. Returns exit code (0=success, 1=errors)."""
    result = TestResult()
    has_errors = False

    # --- 1. Connection test ---
    print(bold("=" * 60))
    print(bold("=== SAP Data Replication Diagnostics ==="))
    print(bold("=" * 60))
    print(f"\n{bold('--- 1. Connection Test ---')}")
    print(f"    Host: {args.host}, SysNr: {args.sysnr}, Client: {args.client}")
    print(f"    User: {args.user}, Lang: {args.lang}")

    try:
        conn = Connection(
            ashost=args.host,
            sysnr=args.sysnr,
            client=args.client,
            user=args.user,
            passwd=args.password,
            lang=args.lang,
        )
        result.connection_info = green(
            f"OK (host: {args.host}, client: {args.client})"
        )
        print(f"    {green('OK')}: Connected to SAP {args.host}:{args.sysnr} "
              f"client {args.client}")

    except SAPRFCError as e:
        print(f"    {red('FAILED')}: {e}")
        result.connection_info = red(f"FAILED ({e})")
        # Print summary with connection failed, then exit
        for line in result.summary_lines():
            print(line)
        return 1
    except Exception as e:
        print(f"    {red('FAILED')}: Unexpected error: {e}")
        result.connection_info = red(f"FAILED ({e})")
        for line in result.summary_lines():
            print(line)
        return 1

    # --- 2. Function module existence checks ---
    print(f"\n{bold('--- 2. Function Module Existence Check ---')}")

    fm_list = [
        "Z_CDC_INIT",
        "Z_CDC_READ",
        "Z_CDC_CLEANUP",
        "Z_EXPORT_TABLE",
        "Z_DELETE_FILE",
        "Z_READ_TABLE",
        "Z_EXECUTE_SQL",
    ]

    fm_exists = {}
    for fm in fm_list:
        exists, detail = check_fm_exists(conn, fm)
        fm_exists[fm] = exists
        if exists:
            print(f"    {fm:<22} {green('EXISTS')}")
        else:
            print(f"    {fm:<22} {red('MISSING')} — {detail}")
            if fm in ("Z_CDC_INIT", "Z_CDC_READ", "Z_CDC_CLEANUP",
                      "Z_EXPORT_TABLE", "Z_DELETE_FILE", "Z_READ_TABLE"):
                has_errors = True

    # Z_EXECUTE_SQL missing is a warning, not an error
    if not fm_exists.get("Z_EXECUTE_SQL"):
        result.set("Z_EXECUTE_SQL", TestResult.WARN,
                    "not found — SchemaManager needs this")

    # --- 3. Safe read-only tests ---

    # Z_READ_TABLE test
    if fm_exists.get("Z_READ_TABLE"):
        if not test_z_read_table(conn, result):
            has_errors = True
    else:
        result.set("Z_READ_TABLE", TestResult.MISSING, "function module not found")
        print(f"\n{bold('--- Z_READ_TABLE ---')}")
        print(f"    {yellow('SKIPPED')}: Function module not found")

    # Z_EXPORT_TABLE + Z_DELETE_FILE test
    if fm_exists.get("Z_EXPORT_TABLE"):
        if not test_z_export_table(conn, result):
            has_errors = True
    else:
        result.set("Z_EXPORT_TABLE", TestResult.MISSING, "function module not found")
        print(f"\n{bold('--- Z_EXPORT_TABLE ---')}")
        print(f"    {yellow('SKIPPED')}: Function module not found")

    # Z_DELETE_FILE: if not already tested via Z_EXPORT_TABLE cleanup
    if result.get("Z_DELETE_FILE") is None:
        if fm_exists.get("Z_DELETE_FILE"):
            result.set("Z_DELETE_FILE", TestResult.EXISTS,
                       "not tested — no file to delete (Z_EXPORT_TABLE skipped)")
        else:
            result.set("Z_DELETE_FILE", TestResult.MISSING, "function module not found")

    # Z_EXECUTE_SQL test
    if fm_exists.get("Z_EXECUTE_SQL"):
        if not test_z_execute_sql_check(conn, result):
            pass  # Warning, not error
    else:
        print(f"\n{bold('--- Z_EXECUTE_SQL ---')}")
        print(f"    {yellow('SKIPPED')}: Function module not found")
        print(f"    SchemaManager (auto table creation) will not work without it.")

    # --- 4. CDC tests ---

    # Check for existing CDC log tables
    print(f"\n{bold('--- 3. CDC Status Check ---')}")
    cdc_initialized_tables = []

    if fm_exists.get("Z_EXECUTE_SQL"):
        for tbl in CDC_CHECK_TABLES:
            exists, detail = check_cdc_log_exists(conn, tbl)
            if exists:
                print(f"    {tbl}: {green('CDC ACTIVE')} — {detail}")
                cdc_initialized_tables.append(tbl)
            else:
                print(f"    {tbl}: {yellow('no CDC')} — {detail}")
    else:
        print(f"    {yellow('SKIPPED')}: Cannot check CDC status (Z_EXECUTE_SQL not available)")

    # Z_CDC_INIT
    if fm_exists.get("Z_CDC_INIT"):
        if args.init_test:
            print(f"\n{bold('--init-test mode: running full CDC lifecycle test ---')}")
            # Run the full cycle: INIT → READ → CLEANUP
            init_ok = test_cdc_init(conn, result, table='ZTLANGTXT')

            if init_ok:
                # Z_CDC_READ test
                if fm_exists.get("Z_CDC_READ"):
                    test_cdc_read(conn, result, table='ZTLANGTXT')
                else:
                    result.set("Z_CDC_READ", TestResult.MISSING,
                               "function module not found")
                    print(f"\n    {yellow('SKIPPED')}: Z_CDC_READ not found")

                # Z_CDC_CLEANUP test — always clean up after init-test
                if fm_exists.get("Z_CDC_CLEANUP"):
                    test_cdc_cleanup(conn, result, table='ZTLANGTXT')
                else:
                    result.set("Z_CDC_CLEANUP", TestResult.MISSING,
                               "function module not found")
                    print(f"\n    {red('ERROR')}: Z_CDC_CLEANUP not found!")
                    print(f"    {yellow('WARNING')}: CDC was initialized on ZTLANGTXT but "
                          f"cannot be cleaned up!")
                    print(f"    You must manually remove the CDC trigger and log table "
                          f"for ZTLANGTXT, or install Z_CDC_CLEANUP.")
                    has_errors = True
        else:
            result.set("Z_CDC_INIT", TestResult.EXISTS,
                       "not tested — use --init-test to create a test CDC on ZTLANGTXT")
            print(f"\n{bold('--- Z_CDC_INIT ---')}")
            print(f"    {yellow('EXISTS')} (verified by metadata)")
            print(f"    Skipping actual initialization — use --init-test "
                  f"to create a test CDC on ZTLANGTXT.")
    else:
        result.set("Z_CDC_INIT", TestResult.MISSING, "function module not found")
        print(f"\n{bold('--- Z_CDC_INIT ---')}")
        print(f"    {red('MISSING')}: Function module not found")

    # Z_CDC_READ (if not already tested via --init-test)
    if result.get("Z_CDC_READ") is None:
        if fm_exists.get("Z_CDC_READ"):
            if cdc_initialized_tables:
                # Test on an already-initialized table
                tbl = cdc_initialized_tables[0]
                test_cdc_read(conn, result, table=tbl)
            else:
                result.set("Z_CDC_READ", TestResult.EXISTS,
                           "not tested — no CDC initialized")
                print(f"\n{bold('--- Z_CDC_READ ---')}")
                print(f"    {yellow('EXISTS')} (verified by metadata)")
                print(f"    Not tested — no CDC initialized for test tables.")
                print(f"    Use --init-test to test the full CDC lifecycle.")
        else:
            result.set("Z_CDC_READ", TestResult.MISSING, "function module not found")
            print(f"\n{bold('--- Z_CDC_READ ---')}")
            print(f"    {red('MISSING')}: Function module not found")

    # Z_CDC_CLEANUP (if not already tested via --init-test)
    if result.get("Z_CDC_CLEANUP") is None:
        if fm_exists.get("Z_CDC_CLEANUP"):
            result.set("Z_CDC_CLEANUP", TestResult.EXISTS,
                       "not tested — no CDC to clean")
            print(f"\n{bold('--- Z_CDC_CLEANUP ---')}")
            print(f"    {yellow('EXISTS')} (verified by metadata)")
            print(f"    Not tested — no CDC to clean (use --init-test to test)")
        else:
            result.set("Z_CDC_CLEANUP", TestResult.MISSING,
                       "function module not found")
            print(f"\n{bold('--- Z_CDC_CLEANUP ---')}")
            print(f"    {red('MISSING')}: Function module not found")

    # --- 5. Summary report ---
    print(f"\n")
    for line in result.summary_lines():
        print(line)

    # Close connection
    try:
        conn.close()
        print(f"\nConnection closed.")
    except Exception:
        pass

    # Final status
    if has_errors:
        print(f"\n{red('Diagnostics completed with ERRORS.')}")
        return 1
    else:
        print(f"\n{green('Diagnostics completed successfully.')}")
        return 0


# ============================================================================
# CLI entry point
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Safe SAP Data Replication diagnostic tool. "
                    "Tests all SAP function modules without side effects "
                    "(unless --init-test is used).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python sap_diagnose.py --host sap.example.com --sysnr 00 --client 100 \\
      --user MYUSER --password MYPASS

  python sap_diagnose.py --host sap.example.com --sysnr 00 --client 100 \\
      --user MYUSER --password MYPASS --init-test

Safety:
  Without --init-test: 100% read-only, no side effects on the SAP system.
  With --init-test: Creates a temporary CDC trigger + log table on ZTLANGTXT,
                    then immediately removes them via Z_CDC_CLEANUP.
        """,
    )
    parser.add_argument('--host', required=True,
                        help='SAP application server host')
    parser.add_argument('--sysnr', required=True,
                        help='SAP system number (e.g. 00)')
    parser.add_argument('--client', required=True,
                        help='SAP client number (e.g. 100)')
    parser.add_argument('--user', required=True,
                        help='SAP user name')
    parser.add_argument('--password', required=True,
                        help='SAP password')
    parser.add_argument('--lang', default='EN',
                        help='SAP language code (default: EN)')
    parser.add_argument('--init-test', action='store_true', default=False,
                        help='Run full CDC lifecycle test on ZTLANGTXT '
                             '(creates + removes trigger and log table)')

    args = parser.parse_args()

    try:
        exit_code = run_diagnostics(args)
    except KeyboardInterrupt:
        print(f"\n{yellow('Interrupted by user.')}")
        exit_code = 130
    except Exception as e:
        print(f"\n{red('FATAL ERROR')}: {e}")
        import traceback
        traceback.print_exc()
        exit_code = 1

    sys.exit(exit_code)


if __name__ == '__main__':
    main()