#!/usr/bin/env python3
"""
sap_rfc.py — Pure-Python ctypes-based SAP RFC connector.

A drop-in replacement for pyrfc.Connection that uses ctypes to call
sapnwrfc.dll (Windows) / libsapnwrfc.so (Linux) directly, without any
compiled extension.  Compatible with Python 3.13+ / 3.14.

API-compatible with pyrfc.Connection:
    conn = Connection(ashost=..., sysnr=..., client=..., user=..., passwd=..., lang=...)
    result = conn.call('Z_READ_TABLE', IV_TABLE='MARA', IV_ROWCOUNT=100)
    conn.close()

The result dict contains:
    - Export scalar parameters as keys (strings or ints)
    - Export table parameters as keys with list-of-dict values
"""

import ctypes
import sys
from ctypes import (
    Structure,
    c_void_p,
    c_long,
    c_uint,
    c_wchar,
    c_wchar_p,
    c_ulong,
    c_int,
    POINTER,
    byref,
    create_unicode_buffer,
)

# ============================================================================
# Constants — from SAP NWRFC SDK headers (sapnwrfc.h)
# ============================================================================

RFC_PARAMETER_NAME_LENGTH = 30
RFC_PARAMETER_DESC_TEXT_LENGTH = 79
RFC_PARAMETER_DEFAULT_VALUE_LENGTH = 61
RFC_FIELD_NAME_LENGTH = 30
RFC_FIELD_DESC_TEXT_LENGTH = 79

# RFCTYPE enum
RFCTYPE_NULL = 0
RFCTYPE_CHAR = 1
RFCTYPE_INT = 2
RFCTYPE_INT1 = 3
RFCTYPE_INT2 = 4
RFCTYPE_INT4 = 5
RFCTYPE_FLOAT = 6
RFCTYPE_BCD = 7
RFCTYPE_DATE = 8
RFCTYPE_TIME = 9
RFCTYPE_BYTE = 10
RFCTYPE_TABLE = 11
RFCTYPE_NUM = 12
RFCTYPE_STRING = 13
RFCTYPE_XSTRING = 14
RFCTYPE_DECF16 = 15
RFCTYPE_DECF34 = 16
RFCTYPE_STRUCTURE = 17

# RFCDIRECTION enum (bitfield)
RFC_IMPORT = 0x01
RFC_EXPORT = 0x02
RFC_CHANGING = 0x04
RFC_TABLES = 0x08

# Integer RFC types — use RfcGetInt / RfcSetInt
_INT_TYPES = frozenset({RFCTYPE_INT, RFCTYPE_INT1, RFCTYPE_INT2, RFCTYPE_INT4})

# Buffer size for RfcGetString — ROWDATA can be up to 10 000 chars
_STRING_BUFFER_SIZE = 10000


# ============================================================================
# ctypes Structures
# ============================================================================

class RFC_ERROR_INFO(Structure):
    """RFC_ERROR_INFO from sapnwrfc.h."""
    _fields_ = [
        ("code", c_long),
        ("group", c_long),
        ("key", c_wchar * 128),
        ("message", c_wchar * 512),
        ("abapMsgClass", c_wchar * 21),
        ("abapMsgType", c_wchar * 2),
        ("abapMsgNumber", c_wchar * 4),
        ("abapMsgV1", c_wchar * 51),
        ("abapMsgV2", c_wchar * 51),
        ("abapMsgV3", c_wchar * 51),
        ("abapMsgV4", c_wchar * 51),
    ]


class RFC_CONNECTION_PARAMETER(Structure):
    """RFC_CONNECTION_PARAMETER from sapnwrfc.h."""
    _fields_ = [
        ("name", c_wchar_p),
        ("value", c_wchar_p),
    ]


class RFC_PARAMETER_DESC(Structure):
    """RFC_PARAMETER_DESC from sapnwrfc.h.

    Only the first three fields (name, type, direction) are used by
    this module; the rest are included for layout correctness.
    """
    _fields_ = [
        ("name", c_wchar * (RFC_PARAMETER_NAME_LENGTH + 1)),       # 31 chars
        # ctypes auto-inserts 2 bytes padding to align ``type`` to 4
        ("type", c_uint),                                           # RFCTYPE
        ("direction", c_uint),                                      # RFCDIRECTION
        ("nucLength", c_uint),
        ("ucLength", c_uint),
        ("decimals", c_uint),
        ("defaultText", c_wchar * (RFC_PARAMETER_DEFAULT_VALUE_LENGTH + 1)),
        ("parameterDescText", c_wchar * (RFC_PARAMETER_DESC_TEXT_LENGTH + 1)),
        ("extendedDescription", c_wchar * (RFC_PARAMETER_NAME_LENGTH + 1)),
    ]


class RFC_FIELD_DESC(Structure):
    """RFC_FIELD_DESC from sapnwrfc.h (used for table field discovery)."""
    _fields_ = [
        ("name", c_wchar * (RFC_FIELD_NAME_LENGTH + 1)),           # 31 chars
        ("type", c_uint),
        ("nucLength", c_uint),
        ("ucLength", c_uint),
        ("decimals", c_uint),
        ("fieldDescText", c_wchar * (RFC_FIELD_DESC_TEXT_LENGTH + 1)),
    ]


# ============================================================================
# Exception
# ============================================================================

class SAPRFCError(Exception):
    """Raised when an SAP NWRFC SDK call fails."""
    pass


# ============================================================================
# Connection — drop-in replacement for pyrfc.Connection
# ============================================================================

class Connection:
    """ctypes-based SAP RFC connection (replaces pyrfc.Connection).

    Parameters
    ----------
    ashost : str   – application server host
    sysnr  : str   – system number
    client : str   – SAP client
    user   : str   – user name
    passwd : str   – password
    lang   : str   – language (default 'EN')
    """

    def __init__(self, ashost=None, sysnr=None, client=None,
                 user=None, passwd=None, lang='EN', **kwargs):
        self._ashost = ashost
        self._sysnr = sysnr
        self._client = client
        self._user = user
        self._passwd = passwd
        self._lang = lang

        self._lib = None
        self._connection_handle = None
        self._error = RFC_ERROR_INFO()
        self._has_set_int = False
        self._has_get_int = False
        self._has_field_count = False
        self._has_field_desc = False

        self._load_dll()
        self._setup_prototypes()
        self._connect()

    # ------------------------------------------------------------------
    # DLL loading & prototype setup
    # ------------------------------------------------------------------

    def _load_dll(self):
        """Load the SAP NWRFC shared library."""
        if sys.platform.startswith("win"):
            self._lib = ctypes.windll.LoadLibrary("sapnwrfc.dll")
        else:
            # Linux: libsapnwrfc.so must be in LD_LIBRARY_PATH
            self._lib = ctypes.CDLL("libsapnwrfc.so")

    def _setup_prototypes(self):
        """Declare argument types and return types for all SDK functions."""
        lib = self._lib

        # --- Connection ---
        lib.RfcOpenConnection.argtypes = [
            POINTER(RFC_CONNECTION_PARAMETER), c_ulong, POINTER(RFC_ERROR_INFO),
        ]
        lib.RfcOpenConnection.restype = c_void_p

        lib.RfcCloseConnection.argtypes = [c_void_p, POINTER(RFC_ERROR_INFO)]
        lib.RfcCloseConnection.restype = c_uint

        # --- Function descriptor ---
        lib.RfcGetFunctionDesc.argtypes = [
            c_void_p, c_wchar_p, POINTER(RFC_ERROR_INFO),
        ]
        lib.RfcGetFunctionDesc.restype = c_void_p

        # --- Function create / destroy ---
        lib.RfcCreateFunction.argtypes = [c_void_p, POINTER(RFC_ERROR_INFO)]
        lib.RfcCreateFunction.restype = c_void_p

        lib.RfcDestroyFunction.argtypes = [c_void_p, POINTER(RFC_ERROR_INFO)]
        lib.RfcDestroyFunction.restype = c_uint

        # --- Invoke ---
        lib.RfcInvoke.argtypes = [
            c_void_p, c_void_p, POINTER(RFC_ERROR_INFO),
        ]
        lib.RfcInvoke.restype = c_uint

        # --- Set string ---
        lib.RfcSetString.argtypes = [
            c_void_p, c_wchar_p, c_wchar_p, c_ulong, POINTER(RFC_ERROR_INFO),
        ]
        lib.RfcSetString.restype = c_uint

        # --- Set int (optional, may not exist in all SDK builds) ---
        try:
            lib.RfcSetInt.argtypes = [
                c_void_p, c_wchar_p, c_int, POINTER(RFC_ERROR_INFO),
            ]
            lib.RfcSetInt.restype = c_uint
            self._has_set_int = True
        except AttributeError:
            self._has_set_int = False

        # --- Get string ---
        lib.RfcGetString.argtypes = [
            c_void_p, c_wchar_p, c_wchar_p, c_ulong,
            POINTER(c_ulong), POINTER(RFC_ERROR_INFO),
        ]
        lib.RfcGetString.restype = c_uint

        # --- Get int (optional) ---
        try:
            lib.RfcGetInt.argtypes = [
                c_void_p, c_wchar_p, POINTER(c_int), POINTER(RFC_ERROR_INFO),
            ]
            lib.RfcGetInt.restype = c_uint
            self._has_get_int = True
        except AttributeError:
            self._has_get_int = False

        # --- Tables ---
        lib.RfcGetTable.argtypes = [
            c_void_p, c_wchar_p, POINTER(c_void_p), POINTER(RFC_ERROR_INFO),
        ]
        lib.RfcGetTable.restype = c_uint

        lib.RfcGetRowCount.argtypes = [
            c_void_p, POINTER(c_ulong), POINTER(RFC_ERROR_INFO),
        ]
        lib.RfcGetRowCount.restype = c_uint

        lib.RfcMoveToNextRow.argtypes = [c_void_p, POINTER(RFC_ERROR_INFO)]
        lib.RfcMoveToNextRow.restype = c_uint

        lib.RfcGetCurrentRow.argtypes = [c_void_p, POINTER(RFC_ERROR_INFO)]
        lib.RfcGetCurrentRow.restype = c_void_p

        # --- Parameter introspection ---
        lib.RfcGetParameterCount.argtypes = [
            c_void_p, POINTER(c_uint), POINTER(RFC_ERROR_INFO),
        ]
        lib.RfcGetParameterCount.restype = c_uint

        lib.RfcGetParameterDescByIndex.argtypes = [
            c_void_p, c_uint, POINTER(RFC_PARAMETER_DESC), POINTER(RFC_ERROR_INFO),
        ]
        lib.RfcGetParameterDescByIndex.restype = c_uint

        # --- Field introspection (for table row field discovery) ---
        try:
            lib.RfcGetFieldCount.argtypes = [
                c_void_p, POINTER(c_uint), POINTER(RFC_ERROR_INFO),
            ]
            lib.RfcGetFieldCount.restype = c_uint
            self._has_field_count = True
        except AttributeError:
            self._has_field_count = False

        try:
            lib.RfcGetFieldDescByIndex.argtypes = [
                c_void_p, c_uint, POINTER(RFC_FIELD_DESC), POINTER(RFC_ERROR_INFO),
            ]
            lib.RfcGetFieldDescByIndex.restype = c_uint
            self._has_field_desc = True
        except AttributeError:
            self._has_field_desc = False

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _connect(self):
        """Open the RFC connection."""
        params = [
            ("ASHOST", self._ashost),
            ("SYSNR", self._sysnr),
            ("CLIENT", self._client),
            ("USER", self._user),
            ("PASSWD", self._passwd),
            ("LANG", self._lang),
        ]
        conn_params = (RFC_CONNECTION_PARAMETER * len(params))()
        for i, (k, v) in enumerate(params):
            conn_params[i].name = k
            conn_params[i].value = str(v) if v is not None else ""

        self._connection_handle = self._lib.RfcOpenConnection(
            conn_params, len(params), byref(self._error),
        )
        if not self._connection_handle:
            raise SAPRFCError(self._format_error("RfcOpenConnection"))

    def close(self):
        """Close the connection."""
        if self._connection_handle:
            self._lib.RfcCloseConnection(
                self._connection_handle, byref(self._error),
            )
            self._connection_handle = None

    # ------------------------------------------------------------------
    # Main entry point — .call()
    # ------------------------------------------------------------------

    def call(self, func_name, **params):
        """Call an RFC function module and return export parameters + tables.

        Mirrors pyrfc.Connection.call():
            result = conn.call('Z_READ_TABLE', IV_TABLE='MARA', IV_ROWCOUNT=100)

        Returns a dict with export scalar params (str/int) and export tables
        (list of dict).
        """
        # 1. Get function descriptor
        func_desc = self._lib.RfcGetFunctionDesc(
            self._connection_handle, func_name, byref(self._error),
        )
        if not func_desc:
            raise SAPRFCError(
                self._format_error(f"RfcGetFunctionDesc({func_name})"))

        # 2. Introspect parameter metadata (names, types, directions)
        param_infos = self._get_parameter_infos(func_desc)

        # 3. Create function instance
        func_handle = self._lib.RfcCreateFunction(
            func_desc, byref(self._error),
        )
        if not func_handle:
            raise SAPRFCError(
                self._format_error(f"RfcCreateFunction({func_name})"))

        try:
            # 4. Set import parameters
            for name, value in params.items():
                self._set_parameter(func_handle, name, value)

            # 5. Invoke
            rc = self._lib.RfcInvoke(
                self._connection_handle, func_handle, byref(self._error),
            )
            if rc != 0:
                raise SAPRFCError(
                    self._format_error(f"RfcInvoke({func_name})"))

            # 6. Read export parameters
            result = self._read_export_parameters(
                func_handle, func_name, param_infos,
            )
            return result

        finally:
            # 7. Clean up
            self._lib.RfcDestroyFunction(func_handle, byref(self._error))

    # ------------------------------------------------------------------
    # Parameter introspection
    # ------------------------------------------------------------------

    def _get_parameter_infos(self, func_desc):
        """Enumerate all parameters of the function module.

        Returns a list of dicts: [{'name', 'type', 'direction'}, ...]
        Returns an empty list if introspection fails.
        """
        infos = []
        count = c_uint()
        rc = self._lib.RfcGetParameterCount(
            func_desc, byref(count), byref(self._error),
        )
        if rc != 0:
            return infos

        for i in range(count.value):
            desc = RFC_PARAMETER_DESC()
            rc = self._lib.RfcGetParameterDescByIndex(
                func_desc, i, byref(desc), byref(self._error),
            )
            if rc == 0:
                name = desc.name
                if name:  # skip empty names (possible layout mismatch)
                    infos.append({
                        'name': name,
                        'type': int(desc.type),
                        'direction': int(desc.direction),
                    })
        return infos

    # ------------------------------------------------------------------
    # Set import parameters
    # ------------------------------------------------------------------

    def _set_parameter(self, func_handle, name, value):
        """Set a single import parameter (string or int)."""
        if isinstance(value, bool):
            value = 'X' if value else ''

        if isinstance(value, int) and not isinstance(value, bool):
            # Try RfcSetInt first, fall back to RfcSetString
            if self._has_set_int:
                rc = self._lib.RfcSetInt(
                    func_handle, name, c_int(value), byref(self._error),
                )
                if rc == 0:
                    return
            # Fallback: set as string
            str_val = str(value)
            rc = self._lib.RfcSetString(
                func_handle, name, str_val,
                c_ulong(len(str_val)), byref(self._error),
            )
            if rc != 0:
                raise SAPRFCError(
                    self._format_error(f"RfcSetString({name}={value!r})"))
        else:
            # String parameter
            str_val = str(value) if value is not None else ''
            rc = self._lib.RfcSetString(
                func_handle, name, str_val,
                c_ulong(len(str_val)), byref(self._error),
            )
            if rc != 0:
                # If string set fails and value looks numeric, try RfcSetInt
                if self._has_set_int:
                    try:
                        int_val = int(value)
                        rc2 = self._lib.RfcSetInt(
                            func_handle, name, c_int(int_val),
                            byref(self._error),
                        )
                        if rc2 == 0:
                            return
                    except (ValueError, TypeError):
                        pass
                raise SAPRFCError(
                    self._format_error(f"RfcSetString({name}={value!r})"))

    # ------------------------------------------------------------------
    # Read export parameters
    # ------------------------------------------------------------------

    def _read_export_parameters(self, func_handle, func_name, param_infos):
        """Read all export/changing/table parameters into a result dict."""
        result = {}

        if param_infos:
            for info in param_infos:
                name = info['name']
                rfc_type = info['type']
                direction = info['direction']

                # Skip import-only parameters
                if direction == RFC_IMPORT:
                    continue

                try:
                    if rfc_type == RFCTYPE_TABLE or direction == RFC_TABLES:
                        result[name] = self._read_table(func_handle, name)
                    elif rfc_type in _INT_TYPES and self._has_get_int:
                        result[name] = self._read_int(func_handle, name)
                    else:
                        val = self._read_string(func_handle, name)
                        if val is not None:
                            result[name] = val
                except SAPRFCError:
                    # Skip parameters that fail to read (possible layout issue)
                    pass
        else:
            # Fallback: introspection failed — try the naming convention
            # EV_* = export value, ET_* = export table
            # We try known parameter names from the calling code
            result = self._read_export_fallback(func_handle, func_name)

        return result

    def _read_export_fallback(self, func_handle, func_name):
        """Fallback when introspection is unavailable.

        Attempts to read common export parameter names based on the
        project's EV_*/ET_* naming convention.  Tries RfcGetString first,
        then RfcGetInt, then RfcGetTable for each candidate name.
        """
        result = {}

        # Common scalar export names in this project
        scalar_candidates = [
            'EV_ERROR', 'EV_ROW_COUNT', 'EV_FILE_NAME', 'EV_GAP_DETECTED',
            'EV_RESULT', 'EV_MESSAGE', 'EV_STATUS', 'EV_RETCODE',
        ]
        # Common table export names
        table_candidates = [
            'ET_DATA', 'ET_FIELDS', 'ET_RETURN', 'ET_MESSAGES',
            'ET_ERRORS', 'ET_RESULT',
        ]

        for name in scalar_candidates:
            # Try string first
            val = self._read_string_silent(func_handle, name)
            if val is not None:
                result[name] = val
                continue
            # Try int
            if self._has_get_int:
                val = self._read_int_silent(func_handle, name)
                if val is not None:
                    result[name] = val

        for name in table_candidates:
            table = self._read_table_silent(func_handle, name)
            if table is not None:
                result[name] = table

        return result

    # ------------------------------------------------------------------
    # Read individual parameters
    # ------------------------------------------------------------------

    def _read_string(self, handle, name, buffer_size=_STRING_BUFFER_SIZE):
        """Read a string export parameter.  Returns str or None."""
        buffer = create_unicode_buffer(buffer_size)
        length = c_ulong()
        rc = self._lib.RfcGetString(
            handle, name, buffer, c_ulong(buffer_size),
            byref(length), byref(self._error),
        )
        if rc != 0:
            # Try reading as int
            if self._has_get_int:
                int_val = c_int()
                self._error = RFC_ERROR_INFO()  # fresh error
                rc2 = self._lib.RfcGetInt(
                    handle, name, byref(int_val), byref(self._error),
                )
                if rc2 == 0:
                    return int_val.value
            return None
        return buffer.value

    def _read_string_silent(self, handle, name, buffer_size=_STRING_BUFFER_SIZE):
        """Read a string parameter; return None on any error (no raise)."""
        saved_error = self._error
        self._error = RFC_ERROR_INFO()
        try:
            return self._read_string(handle, name, buffer_size)
        except Exception:
            return None
        finally:
            self._error = saved_error

    def _read_int(self, handle, name):
        """Read an integer export parameter."""
        int_val = c_int()
        rc = self._lib.RfcGetInt(
            handle, name, byref(int_val), byref(self._error),
        )
        if rc != 0:
            # Fallback: try as string
            val = self._read_string(handle, name)
            if val is not None:
                try:
                    return int(val)
                except ValueError:
                    return val
            raise SAPRFCError(self._format_error(f"RfcGetInt({name})"))
        return int_val.value

    def _read_int_silent(self, handle, name):
        """Read an int parameter; return None on any error."""
        saved_error = self._error
        self._error = RFC_ERROR_INFO()
        try:
            int_val = c_int()
            rc = self._lib.RfcGetInt(
                handle, name, byref(int_val), byref(self._error),
            )
            if rc == 0:
                return int_val.value
            return None
        except Exception:
            return None
        finally:
            self._error = saved_error

    # ------------------------------------------------------------------
    # Read table parameters
    # ------------------------------------------------------------------

    def _read_table(self, func_handle, table_name):
        """Read all rows from a table parameter.

        Returns a list of dict (one per row, field names as keys).
        """
        table_handle = c_void_p()
        rc = self._lib.RfcGetTable(
            func_handle, table_name, byref(table_handle), byref(self._error),
        )
        if rc != 0:
            raise SAPRFCError(
                self._format_error(f"RfcGetTable({table_name})"))

        row_count = c_ulong()
        self._lib.RfcGetRowCount(
            table_handle, byref(row_count), byref(self._error),
        )

        result = []
        for i in range(row_count.value):
            if i > 0:
                self._lib.RfcMoveToNextRow(table_handle, byref(self._error))
            row_handle = self._lib.RfcGetCurrentRow(
                table_handle, byref(self._error),
            )
            if not row_handle:
                continue
            row_dict = self._read_row_fields(row_handle)
            result.append(row_dict)

        return result

    def _read_table_silent(self, func_handle, table_name):
        """Read a table; return None if the parameter doesn't exist."""
        saved_error = self._error
        self._error = RFC_ERROR_INFO()
        try:
            table_handle = c_void_p()
            rc = self._lib.RfcGetTable(
                func_handle, table_name, byref(table_handle),
                byref(self._error),
            )
            if rc != 0:
                return None

            row_count = c_ulong()
            self._lib.RfcGetRowCount(
                table_handle, byref(row_count), byref(self._error),
            )

            result = []
            for i in range(row_count.value):
                if i > 0:
                    self._lib.RfcMoveToNextRow(
                        table_handle, byref(self._error))
                row_handle = self._lib.RfcGetCurrentRow(
                    table_handle, byref(self._error))
                if not row_handle:
                    continue
                row_dict = self._read_row_fields(row_handle)
                result.append(row_dict)
            return result
        except Exception:
            return None
        finally:
            self._error = saved_error

    # ------------------------------------------------------------------
    # Read fields from a row (structure handle)
    # ------------------------------------------------------------------

    # Common SAP table field names used in this project — used as a
    # fallback when field introspection is unavailable.
    _COMMON_FIELDS = [
        'ROWDATA', 'FIELDNAME', 'MANDT', 'TABNAME', 'POSITION',
        'INTTYPE', 'INTLEN', 'DECIMALS', 'KEYFLAG', 'VALUE',
        'SIGN', 'OPTION', 'LOW', 'HIGH', 'DATATYPE',
        'LENGTH', 'OUTPUTLEN', 'CONVEXIT', 'SCRTEXT_L', 'SCRTEXT_M',
        'SCRTEXT_S', 'REPTEXT', 'OFFSET', 'CHECKTABLE', 'DOMNAME',
        'DDTEXT', 'REFTABLE', 'REFFIELD', 'LOGFLAG', 'NOTNULL',
        'ROLLNAME', 'AS4LOCAL', 'AS4VERS', 'AS4POS', 'DDLANGUAGE',
    ]

    def _read_row_fields(self, row_handle):
        """Read all fields from a row (structure) handle.

        Tries field introspection first (RfcGetFieldCount +
        RfcGetFieldDescByIndex).  Falls back to trying common field names
        if introspection is unavailable or fails.
        """
        row_dict = {}

        # --- Attempt 1: field introspection on the row handle ---
        if self._has_field_count and self._has_field_desc:
            field_count = c_uint()
            saved_error = self._error
            self._error = RFC_ERROR_INFO()
            rc = self._lib.RfcGetFieldCount(
                row_handle, byref(field_count), byref(self._error),
            )
            self._error = saved_error
            if rc == 0 and field_count.value > 0:
                for j in range(field_count.value):
                    field_desc = RFC_FIELD_DESC()
                    self._error = RFC_ERROR_INFO()
                    rc2 = self._lib.RfcGetFieldDescByIndex(
                        row_handle, j, byref(field_desc),
                        byref(self._error),
                    )
                    if rc2 == 0 and field_desc.name:
                        field_name = field_desc.name
                        val = self._read_string_silent(row_handle, field_name)
                        if val is not None:
                            row_dict[field_name] = val
                if row_dict:
                    return row_dict

        # --- Attempt 2: try common field names ---
        for field_name in self._COMMON_FIELDS:
            val = self._read_string_silent(row_handle, field_name)
            if val is not None:
                row_dict[field_name] = val

        return row_dict

    # ------------------------------------------------------------------
    # Error formatting
    # ------------------------------------------------------------------

    def _format_error(self, operation):
        """Format the last RFC error into a readable string."""
        return (
            f"SAP RFC error during {operation}: "
            f"[code={self._error.code} group={self._error.group}] "
            f"{self._error.message}"
        )

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False