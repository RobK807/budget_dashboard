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

rem An instance already on 8501 is the one failure this launcher used to hide. Closing the
rem console window does not always stop the server, so a second run would find the port
rem taken, let Streamlit quietly bind 8502 instead, and then open the browser at 8501 --
rem showing the *old* process. Every symptom then looks like a code bug: pages fail on
rem columns that exist, and restarting appears to change nothing.
netstat -ano -p tcp | findstr /r /c:"LISTENING" | findstr /c:":8501 " >nul 2>&1
if not errorlevel 1 (
    echo.
    echo   The dashboard is ALREADY RUNNING on port 8501.
    echo.
    echo   That older copy is still serving the browser, and it is running whatever
    echo   version of the code it started with. Stop it before starting a new one:
    echo.
    echo     - switch to its console window and press Ctrl+C, or
    echo     - run  stop.bat  in this folder
    echo.
    pause
    exit /b 1
)

rem Stop the server once the last browser tab closes, so this window does not outlive it.
rem See budget\watchdog.py. Set here rather than in the code so that `streamlit run app.py`
rem from a terminal still behaves like a server.
set BUDGET_EXIT_WHEN_IDLE=1

echo.
echo   Budget dashboard starting at http://127.0.0.1:8501
echo.
echo   Closing the browser tab shuts the dashboard down and closes this window.
echo   Press Ctrl+C to stop it sooner.
echo.

rem Open the browser once the server has had a moment to bind the port. Streamlit is set
rem to headless in .streamlit\config.toml so it does not open a second tab of its own.
start "" /b cmd /c "timeout /t 5 /nobreak >nul && start "" http://127.0.0.1:8501"

rem The port is pinned rather than left to Streamlit. Without this it falls back to the next
rem free one when 8501 is busy, which is what let the browser and the server disagree.
".venv\Scripts\python.exe" -m streamlit run app.py --server.port 8501

rem No pause: the server exits when the last tab closes, and a prompt here would leave the
rem window sitting open afterwards, which is the thing this is meant to stop. A crash still
rem gets a pause below so the reason stays on screen long enough to read.
if errorlevel 1 (
    echo.
    echo   The dashboard stopped with an error. The message above says why.
    echo.
    pause
)
exit /b 0
