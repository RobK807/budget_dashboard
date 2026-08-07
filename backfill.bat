@echo off
rem Add a prior year's workbook to the database. Double-click this file.
rem
rem It asks before writing anything. The first pass is always a dry run: it does the whole
rem import and then rolls it back, so a workbook it cannot read fails here rather than
rem halfway through a real one.
rem
rem It does NOT push. The NAS copy stays as it is until you push from the Sync page.

cd /d "%~dp0"

set "WORKBOOK=%~1"
if "%WORKBOOK%"=="" set "WORKBOOK=K:\Private\Finance\Budget 25-26.xlsm"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo   No virtual environment found in this folder.
    echo.
    echo   Create one first:
    echo       python -m venv .venv
    echo       .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

if not exist "%WORKBOOK%" (
    echo.
    echo   No workbook at:
    echo       %WORKBOOK%
    echo.
    echo   Drag a different .xlsm onto this file to use that one instead.
    echo.
    pause
    exit /b 1
)

echo.
echo   ============================================================
echo    Backfill a prior year - DRY RUN
echo   ============================================================
echo.
echo    Workbook: %WORKBOOK%
echo.
echo    Nothing will be written by this pass.
echo.

".venv\Scripts\python.exe" -m budget.backfill_year --workbook "%WORKBOOK%" --dry-run
if errorlevel 1 (
    echo.
    echo   The dry run did not succeed, so nothing was written. Read the reason above.
    echo.
    pause
    exit /b 1
)

echo.
echo   ============================================================
echo.
echo    The dry run worked. Check the counts above look right.
echo.
echo    Writing for real needs the dashboard CLOSED - it will refuse otherwise.
echo    A snapshot of the database is taken first, beside it.
echo.
set /p CONFIRM="   Write it now? [y/N] "
if /i not "%CONFIRM%"=="y" (
    echo.
    echo   Nothing was written.
    echo.
    pause
    exit /b 0
)

echo.
".venv\Scripts\python.exe" -m budget.backfill_year --workbook "%WORKBOOK%"
if errorlevel 1 (
    echo.
    echo   Nothing was written. Read the reason above.
    echo.
    pause
    exit /b 1
)

echo.
echo   ============================================================
echo    Checking both years against their workbooks
echo   ============================================================
echo.
echo    The older year passing proves the import. The current year passing
echo    proves the join - that backfilling did not move this year's figures.
echo.

".venv\Scripts\python.exe" -m budget.reconcile --workbook "%WORKBOOK%"
".venv\Scripts\python.exe" -m budget.reconcile

echo.
echo   Both should end RECONCILED. If the second did not, stop and say so -
echo   the snapshot taken above is beside your database.
echo.
pause
