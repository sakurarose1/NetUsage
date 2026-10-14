@echo off
:: SPDX-License-Identifier: AGPL-3.0-or-later
:: NetUsage - Copyright (C) 2026 Sakura - https://github.com/sakurarose1/NetUsage
setlocal
title NetUsage uninstall
net session >nul 2>&1
if %errorlevel% neq 0 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY set "PY=python"

echo This removes the hourly task and the desktop shortcut.
choice /c YN /m "Also delete ALL recorded history"
if errorlevel 2 (
  %PY% "%~dp0netusage.py" uninstall
) else (
  %PY% "%~dp0netusage.py" uninstall --purge
)
echo.
pause
