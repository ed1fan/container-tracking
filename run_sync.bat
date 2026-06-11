@echo off
:: Fantasia container-tracking sync
:: Scheduled via Windows Task Scheduler — runs daily

set SCRIPT_DIR=%~dp0
set PYTHON=C:\Python314\python.exe
set PYTHONIOENCODING=utf-8
set PYTHONPATH=C:\Users\edgar.FANTASIA\AppData\Roaming\Python\Python314\site-packages;%PYTHONPATH%
cd /d "%SCRIPT_DIR%"

echo [%DATE% %TIME%] Starting container-tracking sync >> "%SCRIPT_DIR%sync_runner.log"

"%PYTHON%" sync\sync_containers.py >> "%SCRIPT_DIR%sync_runner.log" 2>&1

if %ERRORLEVEL% NEQ 0 (
    echo [%DATE% %TIME%] ERROR: sync exited with code %ERRORLEVEL% >> "%SCRIPT_DIR%sync_runner.log"
) else (
    echo [%DATE% %TIME%] Sync completed successfully >> "%SCRIPT_DIR%sync_runner.log"
)

"%PYTHON%" sync\generate_report.py >> "%SCRIPT_DIR%sync_runner.log" 2>&1
