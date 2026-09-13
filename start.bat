@echo off
rem Copyright 2026 Google LLC
rem
rem Licensed under the Apache License, Version 2.0 (the "License");
rem you may not use this file except in compliance with the License.
rem You may obtain a copy of the License at
rem
rem     http://www.apache.org/licenses/LICENSE-2.0
rem
rem Unless required by applicable law or agreed to in writing, software
rem distributed under the License is distributed on an "AS IS" BASIS,
rem WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
rem See the License for the specific language governing permissions and
rem limitations under the License.

chcp 65001 >nul 2>&1
setlocal EnableDelayedExpansion
title Artemis Mobile Agent UI

cd /d "%~dp0"

echo ======================================================
echo       Artemis Autonomous Mobile Agent UI
echo ======================================================
echo.

rem Run the PowerShell bootstrap with execution policy bypass
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" %*

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo An error occurred while starting Artemis.
    pause
)

