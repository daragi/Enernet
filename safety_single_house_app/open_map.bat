@echo off
chcp 65001 > nul
setlocal enabledelayedexpansion

set "ROOT_DIR=%~dp0"
if "%ROOT_DIR:~-1%"=="\" set "ROOT_DIR=%ROOT_DIR:~0,-1%"

if exist "C:\Program Files\Python312\python.exe" (
    set "PYTHON_EXE=C:\Program Files\Python312\python.exe"
) else (
    set "PYTHON_EXE=python"
)

:: 1. Clean up port 8000 (Force kill to prevent cache and server lock issues)
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8000 ^| findstr LISTENING') do taskkill /f /pid %%a >nul 2>&1
ping 127.0.0.1 -n 2 >nul

:: 2. Find local IP address using ipconfig
set "LOCAL_IP="
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr "IPv4"') do (
    set "ip_val=%%a"
    :: Remove spaces
    set "ip_val=!ip_val: =!"
    if not "!ip_val!"=="" (
        :: Check if it's not loopback or APIPA
        if "!ip_val:~0,4!" neq "127." (
            if "!ip_val:~0,8!" neq "169.254." (
                set "LOCAL_IP=!ip_val!"
                goto :ip_found
            )
        )
    )
)
:ip_found

if "%LOCAL_IP%"=="" set "LOCAL_IP=127.0.0.1"

set "HOST_NAME=%COMPUTERNAME%"
set "HOST_URL=http://%HOST_NAME%:8000/"
set "IP_URL=http://%LOCAL_IP%:8000/"

:: 3. Generate timestamp for cache-busting (format: YYYYMMDDHHMMSS)
for /f "tokens=1-6 delims=/: " %%a in ('echo %date% %time%') do (
    set "YY=%%a"
    set "MM=%%b"
    set "DD=%%c"
    set "HH=%%d"
    set "MIN=%%e"
)
:: Remove leading space from HH if present
set "HH=!HH: =0!"
set "CACHEBUST=!YY!!MM!!DD!!HH!!MIN!"

echo ====================================================================
echo  Enernet Safety Assignment Web Server Launcher
echo ====================================================================
echo.
echo  [URL Guide for Users]
echo  1. Hostname URL: %HOST_URL%
echo  2. Local IP URL: %IP_URL%
echo.
echo  * Admin Page: %HOST_URL%admin
echo ====================================================================
echo.

echo [0/2] Checking Python runtime and dependencies...
echo Python: %PYTHON_EXE%
"%PYTHON_EXE%" -c "import fastapi, uvicorn, pandas, openpyxl, requests, multipart" >nul 2>&1
if errorlevel 1 (
    echo Missing Python dependencies. Installing from requirements.txt...
    "%PYTHON_EXE%" -m pip install -r "%ROOT_DIR%\requirements.txt"
    if errorlevel 1 (
        echo.
        echo [ERROR] Failed to install Python dependencies.
        echo Run manually:
        echo "%PYTHON_EXE%" -m pip install -r "%ROOT_DIR%\requirements.txt"
        pause
        exit /b 1
    )
)

echo [1/2] Starting backend FastAPI server...
start "Enernet Safety Map Server" cmd /k ""%PYTHON_EXE%" "%ROOT_DIR%\app.py" --bind 0.0.0.0 --port 8000"

:: Wait for server to fully start (6 pings = ~5 seconds)
ping 127.0.0.1 -n 6 >nul

echo [2/2] Launching web browser (cache-busting)...
start "" "%HOST_URL%?v=!CACHEBUST!"

echo.
echo [Done] You can share this URL with others: %HOST_URL%
echo (If the URL does not work, try the IP URL: %IP_URL%)
echo.
pause
