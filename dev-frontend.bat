@echo off
SETLOCAL

SET "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%" || exit /b

if exist package-lock.json (
    echo package-lock.json found
) else (
    echo package-lock.json not found, running npm install...
    call npm install --force
)

echo Starting frontend dev server...
call npm run dev

ENDLOCAL
