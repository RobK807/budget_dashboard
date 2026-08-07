@echo off
rem Check the database against the spreadsheets. Double-click this file.
rem It changes nothing -- it only reads.
rem
rem Run it after a backfill, after a big import, or any time the dashboard and the
rem spreadsheet ought to agree and you want to know that they do.

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo   No virtual environment found in this folder.
    echo.
    pause
    exit /b 1
)

set "PRIOR=K:\Private\Finance\Budget 25-26.xlsm"
set FAILED=0

echo.
echo   ============================================================
echo    Reconciliation - nothing is written
echo   ============================================================

if exist "%PRIOR%" (
    echo.
    echo   ---- 2025-26 ----
    echo.
    ".venv\Scripts\python.exe" -m budget.reconcile --workbook "%PRIOR%"
    if errorlevel 1 set FAILED=1
)

echo.
echo   ---- current year ----
echo.
".venv\Scripts\python.exe" -m budget.reconcile
if errorlevel 1 set FAILED=1

echo.
echo   ============================================================
if "%FAILED%"=="1" (
    echo    At least one year did NOT reconcile. Read the section above
    echo    that reported it - each difference says what it is.
) else (
    echo    Both years reconciled.
)
echo   ============================================================
echo.
pause
