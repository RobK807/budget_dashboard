@echo off
rem Report what the dashboard sees when it looks for the database.
rem Double-click this if budget.bat says there is no database. It changes nothing.

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
echo    Budget dashboard - diagnosis
echo   ============================================================
echo.

".venv\Scripts\python.exe" -m budget.diagnose

echo.
pause
