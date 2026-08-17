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

Key design decisions:
    - SAP_UC (SAP Unicode character) is ``unsigned short`` (2 bytes, UTF-16)
      on ALL platforms.  Python's ``c_wchar`` is 4 bytes on Linux, so we use
      ``c_uint16`` everywhere and manually encode/decode UTF-16LE.
    - All C enums (RFC_RC, RFC_ERROR_GROUP, RFC_TYPE, RFC_DIRECTION) are
      ``int`` (4 bytes).  ``c_long``/``c_ulong`` are 8 bytes on 64-bit Linux
      and must NOT be used.
"""

import ctypes
import sys
import os
import logging
from ctypes import (
    Structure,
    c_void_p,
    c_char_p,
    c_uint,
    c_int,
    c_uint8,
    c_uint16,
    POINTER,
    byref,
)

logger = logging.getLogger(__name__)

# ============================================================================
# SAP_UC type and string helpers
# ============================================================================

# SAP NWRFC SDK uses SAP_UC = unsigned short (2 bytes, UTF-16) on ALL platforms.
# Python's c_wchar is 2 bytes on Windows but 4 bytes on Linux (UCS-4/UTF-32).
# Using c_uint16 ensures correct struct layouts and string encoding everywhere.
SAP_UC = c_uint16


def sap_uc_from_str(s: str) -> bytes:
    """Encode Python string to UTF-16LE bytes (null-terminated).

    The result is suitable for passing to SDK functions as ``SAP_UC*``
    via ``c_char_p`` — the SDK reads the bytes as ``unsigned short*``.
    """
    return s.encode('utf-16-le') + b'\x00\x00'


def sap_uc_to_str(b: bytes) -> str:
    """Decode UTF-16LE bytes to Python string (trailing nulls stripped)."""
    return b.decode('utf-16-le').rstrip('\x00')


def _uc_array_to_str(array) -> str:
    """Convert a c_uint16 array (inline SAP_UC) to a Python string.

    Reads until the first null SAP_UC (0x0000) or end of array.
    """
    chars = []
    for val in array:
        if val == 0:
            break
        chars.append(chr(val))
    return ''.join(chars)


def _uc_ptr_to_str(ptr, max_chars: int = 512) -> str:
    """Read a null-terminated SAP_UC string from a raw pointer.

    ``ptr`` is a c_void_p value (integer address) pointing to SAP_UC data.
    """
    if not ptr:
        return ''
    u16 = ctypes.cast(ptr, POINTER(c_uint16))
    chars = []
    for i in range(max_chars):
        val = u16[i]
        if val == 0:
            break
        chars.append(chr(val))
    return ''.join(chars)


# ============================================================================
# Constants — from SAP NWRFC SDK headers (sapnwrfc.h)
# ============================================================================

RFC_PARAMETER_NAME_LENGTH = 30
RFC_FIELD_NAME_LENGTH = 30
RFC_PARAMETER_DEFAULT_VALUE_LENGTH = 30  # 30+1=31 including null terminator

# ---------------------------------------------------------------------------
# RFCTYPE enum — EXACT values from sapnwrfc.h
# ---------------------------------------------------------------------------
RFCTYPE_NULL = 14
RFCTYPE_CHAR = 0
RFCTYPE_INT = 8       # 4-byte int (RFCTYPE_INT IS the 4-byte integer type)
RFCTYPE_INT1 = 10     # 1-byte int
RFCTYPE_INT2 = 9      # 2-byte int
RFCTYPE_FLOAT = 7
RFCTYPE_BCD = 2       # packed decimal (BCD)
RFCTYPE_DATE = 1
RFCTYPE_TIME = 3
RFCTYPE_BYTE = 4      # binary
RFCTYPE_TABLE = 5
RFCTYPE_NUM = 6       # numeric text
RFCTYPE_STRING = 29
RFCTYPE_XSTRING = 30
RFCTYPE_DECF16 = 23
RFCTYPE_DECF34 = 24
RFCTYPE_STRUCTURE = 17

# ---------------------------------------------------------------------------
# RFC_DIRECTION enum (bitfield) — EXACT values from sapnwrfc.h
# ---------------------------------------------------------------------------
RFC_IMPORT = 0x01
RFC_EXPORT = 0x02
RFC_CHANGING = 0x03   # RFC_IMPORT | RFC_EXPORT
RFC_TABLES = 0x07     # 0x04 | RFC_CHANGING

# Integer RFC types — use RfcGetInt / RfcSetInt
_INT_TYPES = frozenset({RFCTYPE_INT, RFCTYPE_INT1, RFCTYPE_INT2})

# Buffer size for RfcGetString — ROWDATA can be up to 10 000 chars
_STRING_BUFFER_SIZE = 10000


# ============================================================================
# ctypes Structures — layouts match sapnwrfc.h exactly
# ============================================================================

class RFC_ERROR_INFO(Structure):
    """RFC_ERROR_INFO from sapnwrfc.h.

    Layout (all platforms):
        code           : RFC_RC          (enum = int,  4 bytes)
        group          : RFC_ERROR_GROUP (enum = int,  4 bytes)
        key            : SAP_UC[128]     (256 bytes)
        message        : SAP_UC[512]     (1024 bytes)
        abapMsgClass   : SAP_UC[21]      (42 bytes)
        abapMsgType    : SAP_UC[2]       (4 bytes)
        abapMsgNumber  : SAP_UC[4]       (8 bytes)
        abapMsgV1..V4  : SAP_UC[51] each (102 bytes each)
    """
    _fields_ = [
        ("code", c_int),
        ("group", c_int),
        ("key", SAP_UC * 128),
        ("message", SAP_UC * 512),
        ("abapMsgClass", SAP_UC * 21),
        ("abapMsgType", SAP_UC * 2),
        ("abapMsgNumber", SAP_UC * 4),
        ("abapMsgV1", SAP_UC * 51),
        ("abapMsgV2", SAP_UC * 51),
        ("abapMsgV3", SAP_UC * 51),
        ("abapMsgV4", SAP_UC * 51),
    ]


class RFC_CONNECTION_PARAMETER(Structure):
    """RFC_CONNECTION_PARAMETER from sapnwrfc.h.

    Both fields are SAP_UC* (pointers to UTF-16LE strings).
    We use c_char_p so ctypes accepts bytes from sap_uc_from_str().
    """
    _fields_ = [
        ("name", c_char_p),
        ("value", c_char_p),
    ]


class RFC_PARAMETER_DESC(Structure):
    """RFC_PARAMETER_DESC from sapnwrfc.h.

    Layout (64-bit):
        name                 : SAP_UC*              (8 bytes)
        type                 : RFC_TYPE (uint)      (4 bytes)
        direction            : RFC_DIRECTION (uint) (4 bytes)
        nucLength            : unsigned             (4 bytes)
        ucLength             : unsigned             (4 bytes)
        decimals             : unsigned             (4 bytes)
        [padding]                                   (4 bytes)
        typeDescHandle       : void*                (8 bytes)
        defaultValueLength   : unsigned             (4 bytes)
        [padding]                                   (4 bytes)
        defaultValue         : SAP_UC*              (8 bytes)
        optional             : RFC_BYTE (uint8)     (1 byte)
        [padding]                                   (7 bytes)
        extendedDescription  : void*                (8 bytes)
    """
    _fields_ = [
        ("name", c_void_p),
        ("type", c_uint),
        ("direction", c_uint),
        ("nucLength", c_uint),
        ("ucLength", c_uint),
        ("decimals", c_uint),
        ("typeDescHandle", c_void_p),
        ("defaultValueLength", c_uint),
        ("defaultValue", c_void_p),
        ("optional", c_uint8),
        ("extendedDescription", c_void_p),
    ]


class RFC_FIELD_DESC(Structure):
    """RFC_FIELD_DESC from sapnwrfc.h.

    Layout (64-bit):
        name                 : SAP_UC*              (8 bytes)
        type                 : RFC_TYPE (uint)      (4 bytes)
        nucLength            : unsigned             (4 bytes)
        nucOffset            : unsigned             (4 bytes)
        ucLength             : unsigned             (4 bytes)
        ucOffset             : unsigned             (4 bytes)
        decimals             : unsigned             (4 bytes)
        typeDescHandle       : void*                (8 bytes)
        extendedDescription  : void*                (8 bytes)
    """
    _fields_ = [
        ("name", c_void_p),
        ("type", c_uint),
        ("nucLength", c_uint),
        ("nucOffset", c_uint),
        ("ucLength", c_uint),
        ("ucOffset", c_uint),
        ("decimals", c_uint),
        ("typeDescHandle", c_void_p),
        ("extendedDescription", c_void_p),
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
        self._has_set_int = False
        self._has_get_int = False
        self._has_field_count = False
        self._has_field_desc = False
        self._has_describe_type = False
        self._has_move_to_first_row = False
        self._closed = False

        self._load_dll()
        self._setup_prototypes()
        self._connect()

    # ------------------------------------------------------------------
    # DLL loading & prototype setup
    # ------------------------------------------------------------------

    def _load_dll(self):
        """Load the SAP NWRFC shared library."""
        try:
            if sys.platform.startswith("win"):
                self._lib = ctypes.windll.LoadLibrary("sapnwrfc.dll")
            else:
                # Linux: libsapnwrfc.so must be in LD_LIBRARY_PATH
                self._lib = ctypes.CDLL("libsapnwrfc.so")
        except OSError as e:
            hint = (
                "LD_LIBRARY_PATH" if not sys.platform.startswith("win")
                else "PATH"
            )
            lib_name = (
                "libsapnwrfc.so" if not sys.platform.startswith("win")
                else "sapnwrfc.dll"
            )
            raise SAPRFCError(
                f"Failed to load SAP NWRFC SDK library '{lib_name}': {e}\n"
                f"Ensure {lib_name} and its dependencies (libicudec.so, "
                f"libsapucum.so, etc.) are in {hint} and that the SAP "
                f"NWRFC SDK is properly installed."
            ) from e

    def _setup_prototypes(self):
        """Declare argument types and return types for all SDK functions."""
        lib = self._lib

        # --- Connection ---
        lib.RfcOpenConnection.argtypes = [
            POINTER(RFC_CONNECTION_PARAMETER), c_uint, POINTER(RFC_ERROR_INFO),
        ]
        lib.RfcOpenConnection.restype = c_void_p

        lib.RfcCloseConnection.argtypes = [c_void_p, POINTER(RFC_ERROR_INFO)]
        lib.RfcCloseConnection.restype = c_uint

        # --- Function descriptor ---
        lib.RfcGetFunctionDesc.argtypes = [
            c_void_p, c_char_p, POINTER(RFC_ERROR_INFO),
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
            c_void_p, c_char_p, c_char_p, c_uint, POINTER(RFC_ERROR_INFO),
        ]
        lib.RfcSetString.restype = c_uint

        # --- Set int (optional — may not exist in all SDK builds) ---
        try:
            lib.RfcSetInt.argtypes = [
                c_void_p, c_char_p, c_int, POINTER(RFC_ERROR_INFO),
            ]
            lib.RfcSetInt.restype = c_uint
            self._has_set_int = True
        except AttributeError:
            self._has_set_int = False

        # --- Get string ---
        lib.RfcGetString.argtypes = [
            c_void_p, c_char_p, POINTER(c_uint16), c_uint,
            POINTER(c_uint), POINTER(RFC_ERROR_INFO),
        ]
        lib.RfcGetString.restype = c_uint

        # --- Get int (optional) ---
        try:
            lib.RfcGetInt.argtypes = [
                c_void_p, c_char_p, POINTER(c_int), POINTER(RFC_ERROR_INFO),
            ]
            lib.RfcGetInt.restype = c_uint
            self._has_get_int = True
        except AttributeError:
            self._has_get_int = False

        # --- Tables ---
        lib.RfcGetTable.argtypes = [
            c_void_p, c_char_p, POINTER(c_void_p), POINTER(RFC_ERROR_INFO),
        ]
        lib.RfcGetTable.restype = c_uint

        lib.RfcGetRowCount.argtypes = [
            c_void_p, POINTER(c_uint), POINTER(RFC_ERROR_INFO),
        ]
        lib.RfcGetRowCount.restype = c_uint

        lib.RfcMoveToNextRow.argtypes = [c_void_p, POINTER(RFC_ERROR_INFO)]
        lib.RfcMoveToNextRow.restype = c_uint

        # --- MoveToFirstRow (optional in very old SDKs) ---
        try:
            lib.RfcMoveToFirstRow.argtypes = [c_void_p, POINTER(RFC_ERROR_INFO)]
            lib.RfcMoveToFirstRow.restype = c_uint
            self._has_move_to_first_row = True
        except AttributeError:
            self._has_move_to_first_row = False

        lib.RfcGetCurrentRow.argtypes = [c_void_p, POINTER(RFC_ERROR_INFO)]
        lib.RfcGetCurrentRow.restype = c_void_p

        # --- Parameter introspection ---
        lib.RfcGetParameterCount.argtypes = [
            c_void_p, POINTER(c_uint), POINTER(RFC_ERROR_INFO),
        ]
        lib.RfcGetParameterCount.restype = c_uint

        lib.RfcGetParameterDescByIndex.argtypes = [
            c_void_p, c_uint, POINTER(RFC_PARAMETER_DESC),
            POINTER(RFC_ERROR_INFO),
        ]
        lib.RfcGetParameterDescByIndex.restype = c_uint

        # --- Type descriptor (for field introspection) ---
        try:
            lib.RfcDescribeType.argtypes = [
                c_void_p, POINTER(RFC_ERROR_INFO),
            ]
            lib.RfcDescribeType.restype = c_void_p
            self._has_describe_type = True
        except AttributeError:
            self._has_describe_type = False

        # --- Field introspection (takes RFC_TYPE_DESC_HANDLE, not row handle) ---
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
                c_void_p, c_uint, POINTER(RFC_FIELD_DESC),
                POINTER(RFC_ERROR_INFO),
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
        # Keep references to encoded bytes so they aren't garbage-collected
        # before RfcOpenConnection reads them.
        encoded = []
        for i, (k, v) in enumerate(params):
            name_b = sap_uc_from_str(k)
            value_b = sap_uc_from_str(str(v) if v is not None else "")
            encoded.append((name_b, value_b))
            conn_params[i].name = name_b
            conn_params[i].value = value_b

        error = RFC_ERROR_INFO()
        self._connection_handle = self._lib.RfcOpenConnection(
            conn_params, c_uint(len(params)), byref(error),
        )
        if not self._connection_handle:
            raise SAPRFCError(self._format_error("RfcOpenConnection", error))

    def close(self):
        """Close the connection."""
        if self._connection_handle and not self._closed:
            error = RFC_ERROR_INFO()
            self._lib.RfcCloseConnection(
                self._connection_handle, byref(error),
            )
            self._connection_handle = None
            self._closed = True

    def __del__(self):
        """Safety net for connection cleanup."""
        try:
            self.close()
        except Exception:
            pass

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
        func_name_bytes = sap_uc_from_str(func_name)
        error = RFC_ERROR_INFO()
        func_desc = self._lib.RfcGetFunctionDesc(
            self._connection_handle, func_name_bytes, byref(error),
        )
        if not func_desc:
            raise SAPRFCError(
                self._format_error(f"RfcGetFunctionDesc({func_name})", error))

        # 2. Introspect parameter metadata (names, types, directions)
        param_infos = self._get_parameter_infos(func_desc)

        # 3. Create function instance
        error = RFC_ERROR_INFO()
        func_handle = self._lib.RfcCreateFunction(
            func_desc, byref(error),
        )
        if not func_handle:
            raise SAPRFCError(
                self._format_error(f"RfcCreateFunction({func_name})", error))

        try:
            # 4. Set import parameters
            for name, value in params.items():
                self._set_parameter(func_handle, name, value)

            # 5. Invoke
            error = RFC_ERROR_INFO()
            rc = self._lib.RfcInvoke(
                self._connection_handle, func_handle, byref(error),
            )
            if rc != 0:
                raise SAPRFCError(
                    self._format_error(f"RfcInvoke({func_name})", error))

            # 6. Read export parameters
            result = self._read_export_parameters(
                func_handle, func_name, param_infos,
            )
            return result

        finally:
            # 7. Clean up
            error = RFC_ERROR_INFO()
            self._lib.RfcDestroyFunction(func_handle, byref(error))

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
        error = RFC_ERROR_INFO()
        rc = self._lib.RfcGetParameterCount(
            func_desc, byref(count), byref(error),
        )
        if rc != 0:
            return infos

        for i in range(count.value):
            desc = RFC_PARAMETER_DESC()
            error = RFC_ERROR_INFO()
            rc = self._lib.RfcGetParameterDescByIndex(
                func_desc, i, byref(desc), byref(error),
            )
            if rc == 0:
                name = _uc_ptr_to_str(desc.name)
                if name:
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
        name_bytes = sap_uc_from_str(name)

        if isinstance(value, bool):
            value = 'X' if value else ''

        if isinstance(value, int) and not isinstance(value, bool):
            # Try RfcSetInt first, fall back to RfcSetString
            if self._has_set_int:
                error = RFC_ERROR_INFO()
                rc = self._lib.RfcSetInt(
                    func_handle, name_bytes, c_int(value), byref(error),
                )
                if rc == 0:
                    return
            # Fallback: set as string
            str_val = str(value)
            value_bytes = sap_uc_from_str(str_val)
            error = RFC_ERROR_INFO()
            rc = self._lib.RfcSetString(
                func_handle, name_bytes, value_bytes,
                c_uint(len(str_val)), byref(error),
            )
            if rc != 0:
                raise SAPRFCError(
                    self._format_error(f"RfcSetString({name}={value!r})", error))
        else:
            # String parameter
            str_val = str(value) if value is not None else ''
            value_bytes = sap_uc_from_str(str_val)
            error = RFC_ERROR_INFO()
            rc = self._lib.RfcSetString(
                func_handle, name_bytes, value_bytes,
                c_uint(len(str_val)), byref(error),
            )
            if rc != 0:
                # If string set fails and value looks numeric, try RfcSetInt
                if self._has_set_int:
                    try:
                        int_val = int(value)
                        error2 = RFC_ERROR_INFO()
                        rc2 = self._lib.RfcSetInt(
                            func_handle, name_bytes, c_int(int_val),
                            byref(error2),
                        )
                        if rc2 == 0:
                            return
                    except (ValueError, TypeError):
                        pass
                raise SAPRFCError(
                    self._format_error(f"RfcSetString({name}={value!r})", error))

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
            result = self._read_export_fallback(func_handle, func_name)

        return result

    def _read_export_fallback(self, func_handle, func_name):
        """Fallback when introspection is unavailable.

        Attempts to read common export parameter names based on the
        project's EV_*/ET_* naming convention.
        """
        result = {}

        # Common scalar export names in this project
        scalar_candidates = [
            'EV_ERROR', 'EV_ROW_COUNT', 'EV_FILE_NAME', 'EV_GAP_DETECTED',
            'EV_RESULT', 'EV_MESSAGE', 'EV_STATUS', 'EV_RETCODE',
            'EV_TRIGGER_EXISTS', 'EV_LAST_LOG_TIME', 'EV_NEXT_SEQ',
            'EV_HAS_MORE', 'EV_DELETED', 'EV_FILE_SIZE',
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

    def _read_string(self, handle, name, buffer_size=_STRING_BUFFER_SIZE,
                     _retried=False):
        """Read a string export parameter.  Returns str, int, or None.

        Handles RFC_BUFFER_TOO_SMALL by retrying with a larger buffer
        (the ``length`` output parameter gives the required size).
        """
        name_bytes = sap_uc_from_str(name)
        buffer = (c_uint16 * buffer_size)()
        length = c_uint()
        error = RFC_ERROR_INFO()
        rc = self._lib.RfcGetString(
            handle, name_bytes, buffer, c_uint(buffer_size),
            byref(length), byref(error),
        )
        if rc != 0:
            # RFC_BUFFER_TOO_SMALL — retry with larger buffer
            if not _retried and length.value > 0 and length.value >= buffer_size:
                return self._read_string(
                    handle, name, length.value + 1, _retried=True)
            # Try reading as int
            if self._has_get_int:
                int_val = c_int()
                error2 = RFC_ERROR_INFO()
                rc2 = self._lib.RfcGetInt(
                    handle, name_bytes, byref(int_val), byref(error2),
                )
                if rc2 == 0:
                    return int_val.value
            return None
        # Success — decode the UTF-16LE buffer
        if length.value > 0:
            raw = bytes(buffer)[:length.value * 2]
            return raw.decode('utf-16-le')
        return ''

    def _read_string_silent(self, handle, name, buffer_size=_STRING_BUFFER_SIZE):
        """Read a string parameter; return None on any error (no raise)."""
        try:
            return self._read_string(handle, name, buffer_size)
        except Exception:
            return None

    def _read_int(self, handle, name):
        """Read an integer export parameter.  Raises SAPRFCError on failure."""
        name_bytes = sap_uc_from_str(name)
        int_val = c_int()
        error = RFC_ERROR_INFO()
        rc = self._lib.RfcGetInt(
            handle, name_bytes, byref(int_val), byref(error),
        )
        if rc != 0:
            # Fallback: try as string
            val = self._read_string(handle, name)
            if val is not None:
                try:
                    return int(val)
                except ValueError:
                    return val
            raise SAPRFCError(self._format_error(f"RfcGetInt({name})", error))
        return int_val.value

    def _read_int_silent(self, handle, name):
        """Read an int parameter; return None on any error."""
        try:
            name_bytes = sap_uc_from_str(name)
            int_val = c_int()
            error = RFC_ERROR_INFO()
            rc = self._lib.RfcGetInt(
                handle, name_bytes, byref(int_val), byref(error),
            )
            if rc == 0:
                return int_val.value
            return None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Read table parameters
    # ------------------------------------------------------------------

    def _read_table(self, func_handle, table_name):
        """Read all rows from a table parameter.

        Returns a list of dict (one per row, field names as keys).
        """
        name_bytes = sap_uc_from_str(table_name)
        table_handle = c_void_p()
        error = RFC_ERROR_INFO()
        rc = self._lib.RfcGetTable(
            func_handle, name_bytes, byref(table_handle), byref(error),
        )
        if rc != 0:
            raise SAPRFCError(
                self._format_error(f"RfcGetTable({table_name})", error))

        row_count = c_uint()
        error = RFC_ERROR_INFO()
        rc = self._lib.RfcGetRowCount(table_handle, byref(row_count), byref(error))
        if rc != 0:
            raise SAPRFCError(
                self._format_error(f"RfcGetRowCount({table_name})", error))

        if row_count.value == 0:
            return []

        # Move to first row before iteration
        if self._has_move_to_first_row:
            error = RFC_ERROR_INFO()
            rc = self._lib.RfcMoveToFirstRow(table_handle, byref(error))
            if rc != 0:
                raise SAPRFCError(
                    self._format_error(
                        f"RfcMoveToFirstRow({table_name})", error))

        result = []
        for i in range(row_count.value):
            if i > 0:
                error = RFC_ERROR_INFO()
                rc = self._lib.RfcMoveToNextRow(table_handle, byref(error))
                if rc != 0:
                    # Stop iteration on error — don't silently skip
                    logger.warning(
                        "RfcMoveToNextRow failed for table %s at row %d: %s",
                        table_name, i, self._format_error("", error),
                    )
                    break

            error = RFC_ERROR_INFO()
            row_handle = self._lib.RfcGetCurrentRow(
                table_handle, byref(error),
            )
            if not row_handle:
                continue
            row_dict = self._read_row_fields(row_handle)
            result.append(row_dict)

        return result

    def _read_table_silent(self, func_handle, table_name):
        """Read a table; return None if the parameter doesn't exist."""
        try:
            name_bytes = sap_uc_from_str(table_name)
            table_handle = c_void_p()
            error = RFC_ERROR_INFO()
            rc = self._lib.RfcGetTable(
                func_handle, name_bytes, byref(table_handle), byref(error),
            )
            if rc != 0:
                return None

            row_count = c_uint()
            error = RFC_ERROR_INFO()
            rc = self._lib.RfcGetRowCount(
                table_handle, byref(row_count), byref(error))
            if rc != 0:
                return None

            if row_count.value == 0:
                return []

            # Move to first row before iteration
            if self._has_move_to_first_row:
                error = RFC_ERROR_INFO()
                rc = self._lib.RfcMoveToFirstRow(table_handle, byref(error))
                if rc != 0:
                    return None

            result = []
            for i in range(row_count.value):
                if i > 0:
                    error = RFC_ERROR_INFO()
                    rc = self._lib.RfcMoveToNextRow(
                        table_handle, byref(error))
                    if rc != 0:
                        break

                error = RFC_ERROR_INFO()
                row_handle = self._lib.RfcGetCurrentRow(
                    table_handle, byref(error))
                if not row_handle:
                    continue
                row_dict = self._read_row_fields(row_handle)
                result.append(row_dict)
            return result
        except Exception:
            return None

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

        Tries field introspection first (RfcDescribeType →
        RfcGetFieldCount + RfcGetFieldDescByIndex).  Falls back to
        trying common field names if introspection is unavailable or fails.
        """
        row_dict = {}

        # --- Attempt 1: field introspection via type descriptor ---
        # RfcGetFieldCount/RfcGetFieldDescByIndex take a RFC_TYPE_DESC_HANDLE
        # (type descriptor), NOT the row handle.  Must call RfcDescribeType
        # first to obtain the type descriptor handle.
        if (self._has_field_count and self._has_field_desc
                and self._has_describe_type):
            error = RFC_ERROR_INFO()
            type_desc = self._lib.RfcDescribeType(row_handle, byref(error))
            if type_desc:
                field_count = c_uint()
                error2 = RFC_ERROR_INFO()
                rc = self._lib.RfcGetFieldCount(
                    type_desc, byref(field_count), byref(error2))
                if rc == 0 and field_count.value > 0:
                    for j in range(field_count.value):
                        field_desc = RFC_FIELD_DESC()
                        error3 = RFC_ERROR_INFO()
                        rc2 = self._lib.RfcGetFieldDescByIndex(
                            type_desc, j, byref(field_desc), byref(error3))
                        if rc2 == 0:
                            field_name = _uc_ptr_to_str(field_desc.name)
                            if field_name:
                                val = self._read_string_silent(
                                    row_handle, field_name)
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

    def _format_error(self, operation, error=None):
        """Format an RFC_ERROR_INFO into a readable string."""
        if error is None:
            return (
                f"SAP RFC error during {operation}: "
                f"(no error info available)"
            )
        message = _uc_array_to_str(error.message)
        key = _uc_array_to_str(error.key)
        return (
            f"SAP RFC error during {operation}: "
            f"[code={error.code} group={error.group} key='{key}'] "
            f"{message}"
        )

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False