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
Write-Host "       Artemis Autonomous Mobile Agent UI           " -ForegroundColor Cyan
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host ""

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

# 1. Refresh PATH with standard toolchain paths
Update-EnvironmentPath

# 2. Check or install uv
if (-not (Test-CommandExists "uv")) {
    Write-Host "[INFO] uv not found. Installing Astral uv..." -ForegroundColor Yellow
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
        Write-Host "[INFO] Auto-installing missing components ($($missingCore -join ', '))..." -ForegroundColor DarkYellow
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
        Write-Host "   [OK] Initialized .env configuration file." -ForegroundColor Green
    } else {
        New-Item -ItemType File -Path ".env" | Out-Null
    }
}

# 5. Synchronize dependencies
Write-Host "   [INFO] Synchronizing Python runtime and project dependencies..." -ForegroundColor Cyan
uv sync --quiet

# 6. Check and build Showcase UI if not already compiled
$ShowcaseIndex = "$RootDir\apps\showcase_ui\dist\frontend\browser\index.html"
$ShowcaseIndexAlt1 = "$RootDir\apps\showcase_ui\dist\browser\index.html"
$ShowcaseIndexAlt2 = "$RootDir\apps\showcase_ui\dist\index.html"
if ((-not (Test-Path $ShowcaseIndex)) -and (-not (Test-Path $ShowcaseIndexAlt1)) -and (-not (Test-Path $ShowcaseIndexAlt2))) {
    if (-not (Test-CommandExists "npm")) {
        if (Test-CommandExists "winget") {
            Write-Host "   [INFO] Node.js/npm not found. Installing Node.js LTS via WinGet..." -ForegroundColor Yellow
            winget install --id OpenJS.NodeJS.LTS -e --accept-source-agreements --accept-package-agreements --silent 2>$null | Out-Null
            Update-EnvironmentPath
        }
    }

    if (Test-CommandExists "npm") {
        Write-Host "   [INFO] Showcase UI build not found. Compiling Angular Showcase UI..." -ForegroundColor Yellow
        Push-Location "$RootDir\apps\showcase_ui"
        try {
            npm install --silent
            $cliNodeVersion = "$RootDir\apps\showcase_ui\node_modules\@angular\cli\src\utilities\node-version.js"
            if (Test-Path $cliNodeVersion) {
                (Get-Content $cliNodeVersion) -replace '22\.22\.3', '22.22.0' | Set-Content $cliNodeVersion
            }
            npm run build
            Write-Host "   [OK] Showcase UI compiled successfully." -ForegroundColor Green
        } catch {
            Write-Host "   [WARN] Failed to build Showcase UI: $_" -ForegroundColor DarkYellow
        } finally {
            Pop-Location
        }
    } else {
        Write-Host "   [WARN] Node.js/npm not found. Please install Node.js (>= 18) to compile Showcase UI." -ForegroundColor DarkYellow
    }
}

# 7. Optionally configure MCP for detected AI IDEs
Write-Host ""
Write-Host "   [INFO] Would you like to configure ARTEMIS MCP & testing rules for your AI IDEs?" -ForegroundColor Cyan
Write-Host "      (Supported: Antigravity, Cursor, Claude Code/Desktop, Codex, OpenClaw, Windsurf)"
$installMcp = "Y"
if ([Console]::IsInputRedirected -eq $false) {
    $response = Read-Host "      Install MCP configuration & rules now? [Y/n]"
    if ($response -ne "") {
        $installMcp = $response
    }
} else {
    $installMcp = "N"
}

if ($installMcp -match "^[Yy]") {
    Write-Host "   Installing MCP server configuration & testing rules..." -ForegroundColor Cyan
    uv run artemis mcp --install all
    if ($LASTEXITCODE -eq 0) {
        Write-Host "   [OK] MCP configuration and rules installed successfully." -ForegroundColor Green
        Write-Host "   [TIP] You can update or re-install anytime with: uv run artemis mcp --install all" -ForegroundColor Cyan
    } else {
        Write-Host "   [WARN] MCP installation failed. Review the error above and retry with: uv run artemis mcp --install all" -ForegroundColor Yellow
    }
} else {
    Write-Host "   [SKIP] You can install MCP anytime later with: uv run artemis mcp --install all" -ForegroundColor Yellow
}
Write-Host ""

# 8. Launch unified Showcase UI & open browser
# Launch via `python -m artemis` (not the `artemis.exe` console-script shim) so the
# long-running server never locks .venv\Scripts\artemis.exe against `uv sync` reinstalls.
Write-Host "   [INFO] Launching Artemis Showcase UI & Admin Console..." -ForegroundColor Green
if ($NoOpen) {
    uv run python -m artemis ui --port $Port --no-open
} else {
    uv run python -m artemis ui --port $Port --open
}
