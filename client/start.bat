@echo off
REM ========================================================================
REM  SAP Data Replication — One-Click Launcher
REM  Double-click this file to start the GUI client.
REM  Dependencies are auto-installed on first run (needs internet).
REM
REM  Prerequisites:
REM    - Python 3.8+ must be installed and in PATH
REM    - SAP NWRFC SDK DLLs in C:\Windows\System32 (sapnwrfc.dll + 3 ICU DLLs)
REM
REM  Note: pyrfc is no longer used. sap_rfc.py (included in this directory)
REM  is a pure-Python ctypes wrapper around sapnwrfc.dll — no pip install needed.
REM  ========================================================================

setlocal EnableDelayedExpansion
title SAP Data Replication

cd /d "%~dp0"

REM --- Check Python ---
where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found in PATH.
    echo Please install Python 3.8+ from https://python.org
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

REM --- Get Python version (major+minor, e.g. 311 for 3.11) ---
for /f "tokens=*" %%v in ('python -c "import sys; print(f'{sys.version_info.major}{sys.version_info.minor}')"') do set PYVER=%%v
echo Detected Python 3.%PYVER:~1%

REM --- Check dependencies ---
echo Checking dependencies...
python -c "import pyodbc, PySide6, paramiko" 2>nul
if errorlevel 1 (
    echo Installing dependencies ^(pyodbc, PySide6, paramiko^)...
    pip install --quiet pyodbc PySide6 paramiko
    if errorlevel 1 (
        echo.
        echo ERROR: Failed to install dependencies.
        echo Try running manually: pip install pyodbc PySide6 paramiko
        pause
        exit /b 1
    )
)

echo All dependencies OK.
echo.

REM --- Start GUI ---
pythonw gui_client.py
if errorlevel 1 (
    echo.
    echo GUI exited with error. Retrying with console for diagnostics:
    echo.
    python gui_client.py
    pause
)

endlocal