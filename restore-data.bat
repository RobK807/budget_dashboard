@echo off
rem Re-apply the data work that lives in scripts rather than in the dashboard.
rem
rem Both steps are idempotent: they check what is already there and write only what is
rem missing, so running this when everything is already in place changes nothing. Each takes
rem its own backup before writing.
rem
rem   1. the savings and investment plan, the gross/net interest flags, the expected
rem      investment return, and the split of transaction 582 into a 30.00 donation and a
rem      5.70 transaction fee
rem   2. clearing the workbook's standing assumptions off months with no payslip
rem
rem Run repair.bat first if diagnose.bat reports damage, and close every dashboard window --
rem both scripts refuse to write while one is open.

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
echo    Budget dashboard - re-apply the stored data work
echo   ============================================================
echo.
echo   Step 1 of 2: savings plan, interest basis, donation split
echo.

".venv\Scripts\python.exe" -m budget.seed_interest_tracker --yes
if errorlevel 1 (
    echo.
    echo   Step 1 did not complete. Nothing further was attempted.
    echo.
    pause
    exit /b 1
)

echo.
echo   Step 2 of 2: clear assumptions from months not yet paid
echo.

".venv\Scripts\python.exe" -m budget.clear_unpaid_assumptions --yes

echo.
echo   Done. Launch the dashboard with budget.bat and check
echo   Settings ^> Savings targets.
echo.
pause
