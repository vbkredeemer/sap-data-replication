@echo off
REM ========================================================================
REM  SAP Data Replication — One-Click Launcher
REM  Double-click this file to start the GUI client.
REM  Dependencies are auto-installed on first run (needs internet).
REM  Prerequisite: Python 3.10+ must be installed and in PATH.
REM  Prerequisite: SAP NWRFC SDK DLLs must be in C:\Windows\System32
REM  ========================================================================

setlocal
title SAP Data Replication

cd /d "%~dp0"

REM --- Check Python ---
where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found in PATH.
    echo Please install Python 3.10+ from https://python.org
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

REM --- Auto-install dependencies on first run ---
echo Checking dependencies...
python -c "import pyrfc, pyodbc, PySide6, paramiko" 2>nul
if errorlevel 1 (
    echo.
    echo First run: installing dependencies...
    echo This may take 1-2 minutes. Please wait.
    echo.
    pip install -q pyrfc pyodbc PySide6 paramiko
    if errorlevel 1 (
        echo.
        echo ERROR: Failed to install dependencies.
        echo Try running manually: pip install pyrfc pyodbc PySide6 paramiko
        pause
        exit /b 1
    )
    echo Dependencies installed successfully.
    echo.
)

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