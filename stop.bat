@echo off
rem Stop the budget dashboard.
rem
rem Closing the console window does not reliably stop the server on Windows -- the python
rem process can outlive it, keep port 8501, and go on serving the browser. A relaunch then
rem finds the port taken and the two disagree about which code is running, which looks like
rem a fault in the dashboard rather than a stale process. This kills whatever holds the port.

setlocal enabledelayedexpansion

set "found="
for /f "tokens=5" %%p in ('netstat -ano -p tcp ^| findstr /r /c:"LISTENING" ^| findstr /c:":8501 "') do (
    set "found=1"
    echo   Stopping the dashboard, process %%p ...
    taskkill /pid %%p /f >nul 2>&1
)

if not defined found (
    echo.
    echo   Nothing is listening on port 8501 -- the dashboard is not running.
    echo.
    pause
    exit /b 0
)

rem Streamlit runs as a parent and a child; killing the listener can leave the other behind,
rem and it is the one that would hold the port again on the next start.
taskkill /f /im python.exe /fi "WINDOWTITLE eq *streamlit*" >nul 2>&1

timeout /t 1 /nobreak >nul
netstat -ano -p tcp | findstr /r /c:"LISTENING" | findstr /c:":8501 " >nul 2>&1
if errorlevel 1 (
    echo.
    echo   Stopped. Port 8501 is free -- budget.bat will start a fresh copy.
) else (
    echo.
    echo   Port 8501 is still in use. Close the console window the dashboard is
    echo   running in, then try again.
)

echo.
pause
