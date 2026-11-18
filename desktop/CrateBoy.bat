@echo off
title Crate Boy
cd /d "%~dp0"
rem ------------------------------------------------------------------
rem  Crate Boy launcher. Put this file in its own folder and double-click.
rem  Every start it fetches the newest app from GitHub (KPqui/crate-boy).
rem  Your data (IN, OUT) is never touched by updates.
rem ------------------------------------------------------------------
echo.
echo   CRATE BOY
echo   Checking for updates...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; try { $z=Join-Path $env:TEMP 'crateboy_main.zip'; $x=Join-Path $env:TEMP 'crateboy_main'; Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/KPqui/crate-boy/archive/refs/heads/main.zip' -OutFile $z -TimeoutSec 60; if (Test-Path $x) { Remove-Item $x -Recurse -Force }; Expand-Archive -Path $z -DestinationPath $x -Force; $src=Join-Path $x 'crate-boy-main\desktop\app'; $new=(Get-Content (Join-Path $src 'VERSION') -Raw).Trim(); $vf=Join-Path (Get-Location) 'app\VERSION'; $old=''; if (Test-Path $vf) { $old=(Get-Content $vf -Raw).Trim() }; if ($new -ne $old) { New-Item -ItemType Directory -Force -Path 'app' | Out-Null; Get-ChildItem -Path $src | Copy-Item -Destination 'app' -Recurse -Force; Copy-Item (Join-Path $x 'crate-boy-main\desktop\CrateBoy.bat') 'CrateBoy.new.bat' -Force; Write-Host ('  Updated to ' + $new) } else { Write-Host ('  Up to date (' + $old + ')') } } catch { Write-Host '  Could not reach GitHub - starting the version you have.' }"
if not exist "app\server.py" (
  echo.
  echo   Could not download Crate Boy. Check the internet connection and try again.
  pause
  exit /b 1
)
for %%d in (IN OUT) do if not exist "%%d" mkdir "%%d"

cd /d "%~dp0app"
set "VENV=%~dp0app\.venv"
if not exist "%VENV%\Scripts\pythonw.exe" goto setup
fc /b "requirements.txt" "%VENV%\.req" >nul 2>nul
if errorlevel 1 goto deps
goto run

:setup
echo.
echo   First start: setting things up once. This takes a few minutes. Keep this window open.
echo.
set "PY="
for %%v in (3.12 3.13 3.11 3.10) do (
  if not defined PY (
    py -%%v -c "import sys" >nul 2>nul && set "PY=py -%%v"
  )
)
for %%v in (312 313 311 310) do (
  if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe" set PY="%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe"
)
if not defined PY (
  echo   No suitable Python found. Installing Python 3.12 with winget...
  winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
  py -3.12 -c "import sys" >nul 2>nul && set "PY=py -3.12"
  if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)
if not defined PY (
  echo.
  echo   Could not install Python 3.12 automatically.
  echo   Please install it from python.org, then double-click CrateBoy.bat again.
  pause
  exit /b 1
)
echo   Using %PY%
if exist "%VENV%" rmdir /s /q "%VENV%"
%PY% -m venv "%VENV%"
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip

:deps
echo   Installing / updating the parts Crate Boy needs...
"%VENV%\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo   Installing failed. The error is shown above.
  echo   Take a screenshot of this window and send it to Claude.
  pause
  exit /b 1
)
copy /y "requirements.txt" "%VENV%\.req" >nul

:run
start "" "%VENV%\Scripts\pythonw.exe" server.py
cd /d "%~dp0"
rem a "Crate Boy" shortcut with the icon, to pin to the taskbar or drag to the desktop
if not exist "Crate Boy.lnk" powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path (Get-Location) 'Crate Boy.lnk')); $s.TargetPath=(Join-Path (Get-Location) 'CrateBoy.bat'); $s.WorkingDirectory=(Get-Location).Path; $s.IconLocation=(Join-Path (Get-Location) 'app\crateboy.ico'); $s.WindowStyle=7; $s.Save()" >nul 2>nul
if exist "CrateBoy.new.bat" (
  fc /b "CrateBoy.new.bat" "CrateBoy.bat" >nul 2>nul && del "CrateBoy.new.bat"
  if exist "CrateBoy.new.bat" start "" /min cmd /c "timeout /t 2 >nul & move /y CrateBoy.new.bat CrateBoy.bat >nul"
)
exit
