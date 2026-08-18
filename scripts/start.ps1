# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

[CmdletBinding()]
param(
    [int]$Port = 8000,
    [switch]$NoOpen = $false
)

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$RootDir = Split-Path -Parent $ScriptDir
Set-Location $RootDir

Write-Host "======================================================" -ForegroundColor Cyan
Write-Host "      ✨ Artemis Autonomous Mobile Agent UI          " -ForegroundColor Cyan
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host ""

function Update-EnvironmentPath {
    $standardDirs = @(
        "$env:LOCALAPPDATA\Microsoft\WinGet\Links",
        "$env:LOCALAPPDATA\Android\Sdk\platform-tools",
        "$env:ProgramFiles\Android\platform-tools",
        "C:\ProgramData\chocolatey\bin",
        "$env:USERPROFILE\scoop\shims",
        "$env:USERPROFILE\.local\bin",
        "$env:USERPROFILE\.cargo\bin"
    )
    $regPath = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
    $currentPaths = ($env:PATH -split ";") + ($regPath -split ";") + $standardDirs | Where-Object { [string]::IsNullOrWhiteSpace($_) -eq $false -and (Test-Path $_) } | Select-Object -Unique
    $env:PATH = $currentPaths -join ";"
}

function Test-CommandExists {
    param([string]$Command)
    $res = Get-Command $Command -ErrorAction SilentlyContinue
    return ($null -ne $res)
}

# 1. Refresh PATH with standard toolchain paths
Update-EnvironmentPath

# 2. Check or install uv
if (-not (Test-CommandExists "uv")) {
    Write-Host "⚡ uv not found. Installing Astral uv..." -ForegroundColor Yellow
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    Update-EnvironmentPath
}

# 3. Check and try installing missing system toolchains (ADB, FFmpeg, scrcpy)
$missingCore = @()
if (-not (Test-CommandExists "adb")) { $missingCore += "adb" }
if (-not (Test-CommandExists "ffmpeg")) { $missingCore += "ffmpeg" }
if (-not (Test-CommandExists "scrcpy")) { $missingCore += "scrcpy" }

if ($missingCore.Count -gt 0) {
    if (Test-CommandExists "winget") {
        Write-Host "⚡ Auto-installing missing components ($($missingCore -join ', '))..." -ForegroundColor DarkYellow
        if ($missingCore -contains "adb") {
            winget install --id Google.PlatformTools -e --accept-source-agreements --accept-package-agreements --silent 2>$null | Out-Null
        }
        if ($missingCore -contains "ffmpeg") {
            winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements --silent 2>$null | Out-Null
        }
        if ($missingCore -contains "scrcpy") {
            winget install --id Genymobile.scrcpy -e --accept-source-agreements --accept-package-agreements --silent 2>$null | Out-Null
        }
        Update-EnvironmentPath
    }
}

# 4. Check or initialize .env
if (-not (Test-Path ".env")) {
    if (Test-Path ".env.example") {
        Copy-Item ".env.example" ".env"
        Write-Host "   ✔ Initialized .env configuration file." -ForegroundColor Green
    } else {
        New-Item -ItemType File -Path ".env" | Out-Null
    }
}

# 5. Synchronize dependencies
Write-Host "   📦 Synchronizing Python runtime and project dependencies..." -ForegroundColor Cyan
uv sync --quiet

# 6. Launch unified Showcase UI & open browser
Write-Host "   🚀 Launching Artemis Showcase UI & Admin Console..." -ForegroundColor Green
if ($NoOpen) {
    uv run artemis ui --port $Port --no-open
} else {
    uv run artemis ui --port $Port --open
}
