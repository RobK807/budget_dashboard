@echo off
rem Rebuild the local database from the NAS master, for when the dashboard will not start
rem and the Sync page cannot be reached. Refuses if there is unpushed work here.

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
echo    Budget dashboard - restore from the NAS
echo   ============================================================
echo.

".venv\Scripts\python.exe" -m budget.restore

echo.
pause
