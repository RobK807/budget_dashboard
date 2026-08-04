@echo off
rem Launch the budget dashboard. Double-click this file, or run it from a terminal.
rem The server runs for as long as this window stays open.

cd /d "%~dp0"

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

echo.
echo   Budget dashboard starting at http://127.0.0.1:8501
echo.
echo   Leave this window open. Press Ctrl+C, or close it, to stop.
echo.

rem Open the browser once the server has had a moment to bind the port. Streamlit is set
rem to headless in .streamlit\config.toml so it does not open a second tab of its own.
start "" /b cmd /c "timeout /t 5 /nobreak >nul && start "" http://127.0.0.1:8501"

".venv\Scripts\python.exe" -m streamlit run app.py

echo.
echo   Dashboard stopped.
pause
