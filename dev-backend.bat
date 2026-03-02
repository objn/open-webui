@echo off
SETLOCAL

SET "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%backend" || exit /b

if exist "%SCRIPT_DIR%.env" (
    copy /Y "%SCRIPT_DIR%.env" .env >nul
)

if not exist ".venv" (
    echo Creating virtual environment...
    python -m venv .venv
)
call .venv\Scripts\activate.bat

set CORS_ALLOW_ORIGIN=http://localhost:5173;http://localhost:8080
if "%PORT%"=="" set PORT=8080


echo Starting backend dev server on port %PORT% (reload enabled)...
uvicorn open_webui.main:app --port %PORT% --host 0.0.0.0 --forwarded-allow-ips "^*" --workers 1 --ws auto

ENDLOCAL
