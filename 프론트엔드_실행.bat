@echo off
cd /d "%~dp0"

REM Works whether this file is in the project root or inside "frontend".
if exist "frontend\package.json" cd frontend

if not exist "node_modules" (
  echo Installing packages for the first time. Please wait...
  call npm install
)

echo.
echo ==============================================
echo   Starting dev server...
echo   The browser will open automatically.
echo   Close this window to stop the server.
echo ==============================================
echo.

call npm run dev -- --open

pause
