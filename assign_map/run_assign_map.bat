@echo off
chcp 65001 > nul
setlocal enabledelayedexpansion
set "ROOT_DIR=%~dp0"
if "%ROOT_DIR:~-1%"=="\" set "ROOT_DIR=%ROOT_DIR:~0,-1%"

for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8000 ^| findstr LISTENING') do taskkill /f /pid %%a >nul 2>&1

set "LOCAL_IP="
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr "IPv4"') do (
    set "ip_val=%%a"
    set "ip_val=!ip_val: =!"
    if not "!ip_val!"=="" (
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

echo ================================================================
echo  assign_map server
echo ================================================================
echo.
echo  Local:   http://127.0.0.1:8000/
echo  Shared:  http://%COMPUTERNAME%:8000/
echo  IP:      http://%LOCAL_IP%:8000/
echo  Admin:   http://%COMPUTERNAME%:8000/admin
echo.
echo ================================================================
echo.

if not exist "%ROOT_DIR%\daejeon_map.html" (
    echo [ERROR] daejeon_map.html not found.
    pause
    exit /b 1
)

if not exist "%ROOT_DIR%\process_geocode.json" (
    echo [ERROR] process_geocode.json not found.
    pause
    exit /b 1
)

if not exist "%ROOT_DIR%\geocodes\geocoding.json" (
    echo [ERROR] geocodes\geocoding.json not found.
    pause
    exit /b 1
)

if not exist "%ROOT_DIR%\uploaded_workbooks" mkdir "%ROOT_DIR%\uploaded_workbooks" >nul 2>&1

echo [INFO] Starting server...
set "PYTHON_EXE="
if exist "C:\Program Files\Python312\python.exe" set "PYTHON_EXE=C:\Program Files\Python312\python.exe"
if "%PYTHON_EXE%"=="" (
    where python >nul 2>&1
    if not errorlevel 1 set "PYTHON_EXE=python"
)

set "RUN_WITH_PYTHON=0"
if not "%PYTHON_EXE%"=="" (
    if exist "%ROOT_DIR%\app.py" set "RUN_WITH_PYTHON=1"
)

if "%RUN_WITH_PYTHON%"=="1" (
    echo [INFO] Running latest app.py with Python.
    "%PYTHON_EXE%" "%ROOT_DIR%\app.py" --bind 0.0.0.0 --port 8000 --open-browser 1>>"%ROOT_DIR%\assign_map_server.log" 2>>"%ROOT_DIR%\assign_map_server_error.log"
) else (
    if not exist "%ROOT_DIR%\assign_map.exe" (
        echo [ERROR] Neither Python app.py nor assign_map.exe is available.
        pause
        exit /b 1
    )
    if not exist "%ROOT_DIR%\_internal" (
        echo [ERROR] _internal folder not found.
        echo Copy the whole assign_map folder, not only assign_map.exe.
        pause
        exit /b 1
    )
    echo [INFO] Python not found. Running bundled assign_map.exe.
    "%ROOT_DIR%\assign_map.exe" --bind 0.0.0.0 --port 8000 1>>"%ROOT_DIR%\assign_map_server.log" 2>>"%ROOT_DIR%\assign_map_server_error.log"
)
echo.
echo [ERROR] Server stopped. Check these files:
echo   %ROOT_DIR%\assign_map_server.log
echo   %ROOT_DIR%\assign_map_server_error.log
pause
