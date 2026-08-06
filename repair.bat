@echo off
rem Rebuild damaged indexes in the local database.
rem
rem Run this when diagnose.bat reports something like
rem     wrong # of entries in index sqlite_autoindex_setting_1
rem That is damage to an index, not to the rows, and rebuilding the indexes from the rows
rem fixes it without losing anything. A backup is taken first either way.
rem
rem Close every dashboard window before running it.

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo   No virtual environment found in this folder.
    echo.
    pause
    exit /b 1
)

echo.
echo   ============================================================
echo    Budget dashboard - repair
echo   ============================================================

".venv\Scripts\python.exe" -m budget.repair

echo.
pause
