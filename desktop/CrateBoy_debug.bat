@echo off
rem Crate Boy with a visible console. Use this if the window does not open: it shows the error.
cd /d "%~dp0app"
if not exist ".venv\Scripts\python.exe" (
  echo Run CrateBoy.bat once first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" server.py
echo.
echo Crate Boy stopped.
pause
