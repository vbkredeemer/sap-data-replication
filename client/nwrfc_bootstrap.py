"""
NWRFC DLL Bootstrap
====================
Ensures the 4 SAP NWRFC SDK DLLs are available before sap_rfc is imported.

The 4 required DLLs (from SAP NWRFC SDK 7.50, Windows x64):
  - sapnwrfc.dll    — SAP RFC runtime
  - icudt57.dll       — ICU Data (Unicode)
  - icuin57.dll       — ICU Internationalization
  - icuuc57.dll       — ICU Common Utilities

Strategy:
  1. Check if DLLs are already in C:\\Windows\\System32 → done, nothing to do.
  2. Check if DLLs are next to this executable (frozen exe or script dir).
  3. If found locally but NOT in System32:
     a. Try to copy them to System32 (needs admin rights).
     b. If not admin, trigger a UAC elevation prompt to copy them.
     c. If user declines, add the local directory to the DLL search path
        as a fallback (works for sap_rfc, but less robust than System32).
  4. If DLLs are nowhere to be found, show a clear error message with
     download instructions.

This module must be imported BEFORE any sap_rfc import.
"""

import ctypes
import os
import sys
import shutil
import subprocess
from pathlib import Path

# The 4 NWRFC DLLs that cannot be redistributed
NWRFC_DLLS = [
    "sapnwrfc.dll",
    "icudt57.dll",
    "icuin57.dll",
    "icuuc57.dll",
]

SYSTEM32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"


def _is_frozen() -> bool:
    """True if running as PyInstaller exe."""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def _get_app_dir() -> Path:
    """Directory where the exe or script lives."""
    if _is_frozen():
        return Path(sys.executable).parent
    return Path(__file__).parent.resolve()


def _is_admin() -> bool:
    """Check if running with administrator privileges."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def _elevate_and_copy(src_dir: Path, dlls_to_copy: list) -> bool:
    """
    Relaunch the current process with UAC elevation to copy DLLs to System32.
    Returns True if the elevated process succeeded.
    """
    if not _is_frozen():
        # Running as script — can't easily self-elevate a Python script
        # Fall back to adding to DLL search path
        return False

    # Build a small inline script that copies the DLLs, then relaunches the app
    # We use a batch approach: copy DLLs, then start the original exe again
    exe = sys.executable
    app_dir = _get_app_dir()

    # PowerShell command to copy DLLs with elevation, then restart the app
    dll_copy_commands = "; ".join(
        f"Copy-Item -Path '{src_dir / dll}' -Destination '{SYSTEM32 / dll}' -Force"
        for dll in dlls_to_copy
    )
    restart_cmd = f"Start-Process '{exe}'"

    ps_script = (
        f"Start-Process powershell -Verb RunAs -ArgumentList "
        f"'-Command', '{dll_copy_commands}; {restart_cmd}; Stop-Process'"
    )

    try:
        subprocess.Popen(
            ["powershell", "-Command", ps_script],
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        # The elevated process will restart us; exit this instance
        sys.exit(0)
    except Exception:
        return False


def _add_to_dll_path(directory: Path):
    """Add a directory to the DLL search path for the current process."""
    try:
        # Use SetDllDirectoryW — sap_rfc/libsapnwrfc will find DLLs there
        ctypes.windll.kernel32.SetDllDirectoryW(str(directory))
    except Exception:
        # Fallback: add to PATH
        os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")


def _check_dlls_in_dir(directory: Path) -> list:
    """Return list of NWRFC DLLs found in the given directory."""
    return [dll for dll in NWRFC_DLLS if (directory / dll).is_file()]


def ensure_nwrfc_dlls() -> tuple:
    """
    Ensure NWRFC DLLs are available.

    Returns:
        (status, message) where status is one of:
        - "ok"           — DLLs found in System32, all good
        - "copied"       — DLLs were copied to System32 (this run)
        - "elevated"     — UAC elevation triggered, app will restart
        - "fallback"     — DLLs found locally, using DLL path fallback
        - "missing"      — DLLs not found anywhere, error
    """
    # Not on Windows — nothing to do
    if not sys.platform.startswith("win"):
        return ("ok", "Not Windows — assuming libsapnwrfc.so is in LD_LIBRARY_PATH")

    # 1. Check System32
    in_system32 = _check_dlls_in_dir(SYSTEM32)
    if len(in_system32) == len(NWRFC_DLLS):
        return ("ok", f"All {len(NWRFC_DLLS)} NWRFC DLLs found in System32")

    # 2. Check app directory (next to exe or script)
    app_dir = _get_app_dir()
    in_app_dir = _check_dlls_in_dir(app_dir)

    if len(in_app_dir) == 0:
        # No DLLs locally — check if some are in System32 but not all
        missing = set(NWRFC_DLLS) - set(in_system32)
        return (
            "missing",
            f"NWRFC SDK DLLs not found. Missing: {', '.join(sorted(missing))}\n"
            f"Please copy these 4 DLLs from the SAP NWRFC SDK 7.50 (nwrfcsdk\\bin\\) "
            f"either next to this executable ({app_dir}) or to {SYSTEM32}:\n"
            + "\n".join(f"  - {dll}" for dll in NWRFC_DLLS)
        )

    # 3. Some DLLs found locally — try to get them to System32
    missing_in_sys32 = [dll for dll in NWRFC_DLLS if dll not in in_system32]
    missing_local = [dll for dll in NWRFC_DLLS if dll not in in_app_dir]

    if missing_local:
        return (
            "missing",
            f"Only {len(in_app_dir)} of {len(NWRFC_DLLS)} NWRFC DLLs found locally. "
            f"Missing: {', '.join(sorted(missing_local))}\n"
            f"Copy all 4 DLLs next to this executable ({app_dir}):\n"
            + "\n".join(f"  - {dll}" for dll in NWRFC_DLLS)
        )

    # All 4 DLLs are in app_dir but not (all) in System32
    # Try copying to System32
    if _is_admin():
        try:
            for dll in NWRFC_DLLS:
                shutil.copy2(app_dir / dll, SYSTEM32 / dll)
            return ("copied", f"Copied all 4 NWRFC DLLs to {SYSTEM32}")
        except Exception as e:
            # Copy failed — use fallback
            _add_to_dll_path(app_dir)
            return ("fallback", f"Copy to System32 failed ({e}), using local DLL path")

    # Not admin — try UAC elevation (frozen exe only)
    if _is_frozen():
        if _elevate_and_copy(app_dir, NWRFC_DLLS):
            return ("elevated", "Requesting admin rights to copy DLLs to System32. "
                    "The app will restart automatically after copying.")

    # Fallback: add app dir to DLL search path
    _add_to_dll_path(app_dir)
    return ("fallback",
            f"NWRFC DLLs found in {app_dir}, added to DLL search path. "
            f"For better stability, copy them to {SYSTEM32} manually "
            f"(run as administrator).")


def bootstrap():
    """
    Main entry point. Call this at the very start of the application,
    before any sap_rfc import.

    On Windows: shows a message box if there are issues (when frozen).
    Returns (status, message).
    """
    status, message = ensure_nwrfc_dlls()

    if status in ("missing",):
        # Show error dialog if frozen (GUI), otherwise print to stderr
        if _is_frozen():
            try:
                ctypes.windll.user32.MessageBoxW(
                    0,
                    message + "\n\nThe application cannot start without these DLLs.",
                    "SAP NWRFC SDK DLLs Missing",
                    0x10  # MB_ICONERROR
                )
            except Exception:
                pass
        else:
            print(f"ERROR: {message}", file=sys.stderr)
        return (status, message)

    if status in ("copied", "fallback", "elevated") and _is_frozen():
        # Brief info for the user
        try:
            icon = 0x40  # MB_ICONINFORMATION
            ctypes.windll.user32.MessageBoxW(
                0,
                message,
                "SAP NWRFC DLL Setup",
                icon
            )
        except Exception:
            pass

    return (status, message)