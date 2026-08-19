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

<#
.SYNOPSIS
    Artemis Smart Multi-Platform Dependency Installer for Windows (PowerShell).
#>

[CmdletBinding()]
param(
    [switch]$Launch = $false,
    [switch]$Open = $false
)

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "======================================================" -ForegroundColor Cyan
Write-Host "   🚀 Artemis - Windows Smart Installer               " -ForegroundColor Cyan
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host ""

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$RootDir = Split-Path -Parent $ScriptDir
Set-Location $RootDir

function Update-EnvironmentPath {
    $standardDirs = @(
        "$env:LOCALAPPDATA\Microsoft\WinGet\Links",
        "$env:LOCALAPPDATA\Android\Sdk\platform-tools",
        "$env:ProgramFiles\Android\platform-tools",
        "$env:ProgramFiles\nodejs",
        "${env:ProgramFiles(x86)}\nodejs",
        "$env:APPDATA\npm",
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

# 0. Initial PATH refresh
Update-EnvironmentPath

# 1. Check System Dependencies (ADB, FFmpeg, scrcpy)
Write-Host "1. Checking System Toolchains (ADB, FFmpeg, scrcpy)..." -ForegroundColor Yellow
$missingTools = @()

if (-not (Test-CommandExists "adb")) { $missingTools += "adb" }
if (-not (Test-CommandExists "ffmpeg")) { $missingTools += "ffmpeg" }
if (-not (Test-CommandExists "scrcpy")) { $missingTools += "scrcpy" }

if ($missingTools.Count -eq 0) {
    Write-Host "   ✔ All system toolchains are already installed (ADB, FFmpeg, scrcpy)." -ForegroundColor Green
} else {
    Write-Host "   ! Missing toolchains: $($missingTools -join ', ')" -ForegroundColor DarkYellow
    Write-Host "   Attempting automatic installation via Windows Package Manager..." -ForegroundColor Gray

    if (Test-CommandExists "winget") {
        Write-Host "   Detected WinGet. Installing packages..." -ForegroundColor Cyan
        if ($missingTools -contains "adb") {
            Write-Host "   📦 Installing Google.PlatformTools (ADB)..." -ForegroundColor Cyan
            winget install --id Google.PlatformTools -e --accept-source-agreements --accept-package-agreements --silent 2>$null | Out-Null
        }
        if ($missingTools -contains "ffmpeg") {
            Write-Host "   📦 Installing Gyan.FFmpeg..." -ForegroundColor Cyan
            winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements --silent 2>$null | Out-Null
        }
        if ($missingTools -contains "scrcpy") {
            Write-Host "   📦 Installing Genymobile.scrcpy..." -ForegroundColor Cyan
            winget install --id Genymobile.scrcpy -e --accept-source-agreements --accept-package-agreements --silent 2>$null | Out-Null
        }
    } elseif (Test-CommandExists "choco") {
        Write-Host "   Detected Chocolatey. Installing packages..." -ForegroundColor Cyan
        choco install ($missingTools -join " ") -y | Out-Null
    } elseif (Test-CommandExists "scoop") {
        Write-Host "   Detected Scoop. Installing packages..." -ForegroundColor Cyan
        scoop install ($missingTools -join " ") | Out-Null
    }

    Update-EnvironmentPath
}

# 2. Check and Install uv (Fast Python Package Manager)
Write-Host "`n2. Checking Python Package Manager (uv)..." -ForegroundColor Yellow
if (-not (Test-CommandExists "uv")) {
    Write-Host "   uv not found. Installing Astral uv..." -ForegroundColor Cyan
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    Update-EnvironmentPath
}

if (Test-CommandExists "uv") {
    $uvVer = uv --version
    Write-Host "   ✔ uv is ready ($uvVer)" -ForegroundColor Green
} else {
    Write-Host "   ✗ Failed to detect uv in PATH." -ForegroundColor Red
    Exit 1
}

# 3. Setup Python Runtime & Sync Dependencies
Write-Host "`n3. Configuring Python Runtime & Syncing Dependencies..." -ForegroundColor Yellow
Write-Host "   📦 Running uv sync (auto-provisioning Python >=3.12 & dependencies)..." -ForegroundColor Cyan
uv sync
Write-Host "   ✔ Dependencies synced successfully." -ForegroundColor Green

# 4. Check .env Configuration
Write-Host "`n4. Checking Environment Configuration (.env)..." -ForegroundColor Yellow
if (-not (Test-Path ".env")) {
    if (Test-Path ".env.example") {
        Copy-Item ".env.example" ".env"
        Write-Host "   ✔ Created .env template from .env.example." -ForegroundColor Green
    } else {
        New-Item -ItemType File -Path ".env" | Out-Null
        Write-Host "   ✔ Created empty .env file." -ForegroundColor Green
    }
} else {
    Write-Host "   ✔ .env configuration file exists." -ForegroundColor Green
}

# 5. Check and Build Showcase UI (Angular)
Write-Host "`n5. Checking Showcase UI Build (Angular)..." -ForegroundColor Yellow
$ShowcaseIndex = "$RootDir\apps\showcase_ui\dist\frontend\browser\index.html"
$ShowcaseIndexAlt1 = "$RootDir\apps\showcase_ui\dist\browser\index.html"
$ShowcaseIndexAlt2 = "$RootDir\apps\showcase_ui\dist\index.html"
if ((-not (Test-Path $ShowcaseIndex)) -and (-not (Test-Path $ShowcaseIndexAlt1)) -and (-not (Test-Path $ShowcaseIndexAlt2))) {
    if (-not (Test-CommandExists "npm")) {
        if (Test-CommandExists "winget") {
            Write-Host "   ⚡ Node.js/npm not found. Installing Node.js LTS via WinGet..." -ForegroundColor Cyan
            winget install --id OpenJS.NodeJS.LTS -e --accept-source-agreements --accept-package-agreements --silent 2>$null | Out-Null
            Update-EnvironmentPath
        }
    }

    if (Test-CommandExists "npm") {
        Write-Host "   🎨 Compiling Angular Showcase UI..." -ForegroundColor Cyan
        Push-Location "$RootDir\apps\showcase_ui"
        try {
            npm install --silent
            npm run build
            Write-Host "   ✔ Showcase UI compiled successfully." -ForegroundColor Green
        } catch {
            Write-Host "   ⚠ Failed to build Showcase UI: $_" -ForegroundColor DarkYellow
        } finally {
            Pop-Location
        }
    } else {
        Write-Host "   ⚠ Could not find npm. Showcase UI will show fallback notice on launch." -ForegroundColor DarkYellow
    }
} else {
    Write-Host "   ✔ Showcase UI build already exists." -ForegroundColor Green
}

# 6. Toolchain Readiness Summary
Write-Host "`n6. Toolchain Readiness Summary:" -ForegroundColor Yellow
$tools = @("adb", "ffmpeg", "scrcpy", "uv", "npm")
foreach ($t in $tools) {
    if (Test-CommandExists $t) {
        $p = (Get-Command $t).Source
        Write-Host "   ✔ $t -> $p" -ForegroundColor Green
    } else {
        Write-Host "   ○ $t -> Not found in PATH (Fallbacks active)" -ForegroundColor DarkYellow
    }
}

Write-Host ""
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host "   ✨ Artemis Environment Ready!                      " -ForegroundColor Green
Write-Host "======================================================" -ForegroundColor Cyan

if ($Launch -or $Open) {
    Write-Host "🚀 Launching Showcase UI..." -ForegroundColor Green
    uv run artemis ui --open
} else {
    Write-Host "To start the Showcase UI and interactive onboarding:" -ForegroundColor White
    Write-Host "  👉 .\start.bat   (or: uv run artemis ui)" -ForegroundColor Cyan
    Write-Host ""
}

