@echo off
:: SPDX-License-Identifier: AGPL-3.0-or-later
:: NetUsage - Copyright (C) 2026 Sakura - https://github.com/sakurarose1/NetUsage
setlocal
title NetUsage setup

:: Ask for administrator rights (needed to read Windows' usage log and create the hourly task)
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Asking for administrator permission...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo.
  echo Python was not found. Install it from https://www.python.org/downloads/
  echo ^(tick "Add python.exe to PATH" during setup^), then run Install.bat again.
  echo.
  pause
  exit /b 1
)

if not exist "%~dp0lib\dissect\esedb\__init__.py" (
  echo Downloading the libraries NetUsage needs ^(about 2 MB, one time only^)...
  %PY% -m pip install --quiet --disable-pip-version-check --no-deps --target "%~dp0lib" -r "%~dp0requirements.txt"
  if errorlevel 1 (
    echo.
    echo Could not download the libraries. Check your internet connection and try again,
    echo or download the ready-made zip from https://github.com/sakurarose1/NetUsage/releases
    echo.
    pause
    exit /b 1
  )
)

%PY% "%~dp0netusage.py" install
echo.
pause
