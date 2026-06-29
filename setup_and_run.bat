@echo off
REM ===========================================================================
REM   Accounting Automation - Bank Module
REM   One-click setup and run for Windows.
REM
REM   First run: creates venv, installs deps, seeds the local SQLite DB,
REM   runs tests, starts the web server, opens your browser.
REM   Subsequent runs: just starts the server and opens the browser.
REM ===========================================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo ===============================================================
echo   Accounting Automation - Bank Module
echo ===============================================================
echo.

REM ---------- Python check ----------
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python is not installed or not on your PATH.
    echo   1. Download Python 3.10+ from https://www.python.org/downloads/
    echo   2. During install, CHECK "Add Python to PATH".
    echo   3. Re-run this script.
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYV=%%v
echo Python detected: !PYV!

REM ---------- venv ----------
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment in .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Could not create virtual environment.
        pause
        exit /b 1
    )
)

REM ---------- deps ----------
echo Installing dependencies ^(may take 1-2 minutes on first run^) ...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] pip install failed. Check your internet connection.
    pause
    exit /b 1
)

REM ---------- tests ----------
if not exist ".tests_passed" (
    echo.
    echo Running tests against the parser, matcher, classifier, and JE generator ...
    ".venv\Scripts\python.exe" -m pytest tests/ -v --override-ini="cache_dir=%TEMP%\pytest_cache_acct"
    if errorlevel 1 (
        echo.
        echo [ERROR] Tests failed - not launching the app. Share the output above.
        pause
        exit /b 1
    )
    type nul > .tests_passed
)

REM ---------- launch ----------
echo.
echo ===============================================================
echo   Starting the local web server at http://localhost:8000
echo   Closing this window will stop the server.
echo   Press Ctrl+C in this window to shut down cleanly.
echo ===============================================================
echo.

REM Open browser after a short delay so the server has time to bind
start "" /min cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:8000/"

REM Run the server in this window (blocks until you close it)
".venv\Scripts\python.exe" -m uvicorn app.main:app --port 8000 --host 127.0.0.1

echo.
echo Server stopped. Press any key to close.
pause >nul
endlocal
