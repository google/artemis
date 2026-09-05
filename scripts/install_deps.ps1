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
        "$env:LOCALAPPDATA\Android\android-sdk\platform-tools",
        "$env:ProgramFiles\Android\platform-tools",
        "${env:ProgramFiles(x86)}\Android\android-sdk\platform-tools",
        "$env:ProgramFiles\nodejs",
        "${env:ProgramFiles(x86)}\nodejs",
        "$env:APPDATA\npm",
        "$env:LOCALAPPDATA\Programs\platform-tools",
        "$env:LOCALAPPDATA\Programs\node",
        "$env:USERPROFILE\.local\share\platform-tools",
        "$env:USERPROFILE\.local\share\node",
        "$env:LOCALAPPDATA\nvm",
        "$env:ProgramData\nvm",
        "C:\ProgramData\chocolatey\bin",
        "$env:USERPROFILE\scoop\shims",
        "$env:USERPROFILE\.local\bin",
        "$env:USERPROFILE\.cargo\bin"
    )
    if ($env:ANDROID_HOME) { $standardDirs += "$env:ANDROID_HOME\platform-tools" }
    if ($env:ANDROID_SDK_ROOT) { $standardDirs += "$env:ANDROID_SDK_ROOT\platform-tools" }
    if ($env:NVM_HOME) { $standardDirs += $env:NVM_HOME }
    if ($env:NVM_SYMLINK) { $standardDirs += $env:NVM_SYMLINK }

    $regPath = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
    $currentPaths = ($env:PATH -split ";") + ($regPath -split ";") + $standardDirs | Where-Object { [string]::IsNullOrWhiteSpace($_) -eq $false -and (Test-Path $_) } | Select-Object -Unique
    $env:PATH = $currentPaths -join ";"
}

function Start-LocalAdbServer {
    if (Test-CommandExists "adb") {
        try {
            $origSocket = $env:ADB_SERVER_SOCKET
            Remove-Item Env:\ADB_SERVER_SOCKET -ErrorAction SilentlyContinue
            & adb start-server 2>$null | Out-Null
            if ($origSocket) { $env:ADB_SERVER_SOCKET = $origSocket }
        } catch {}
    }
}

function Test-CommandExists {
    param([string]$Command)
    $res = Get-Command $Command -ErrorAction SilentlyContinue
    return ($null -ne $res)
}

function Install-PortablePlatformTools {
    $ptDir = "$env:LOCALAPPDATA\Programs\platform-tools"
    if (Test-Path "$ptDir\adb.exe") {
        $env:PATH = "$ptDir;$env:PATH"
        return $true
    }
    Write-Host "   [INFO] Installing Android platform-tools (adb) in user space..." -ForegroundColor Cyan
    try {
        $zipPath = "$env:TEMP\platform-tools-windows.zip"
        Invoke-WebRequest -Uri "https://dl.google.com/android/repository/platform-tools-latest-windows.zip" -OutFile $zipPath -UseBasicParsing
        New-Item -ItemType Directory -Path "$env:LOCALAPPDATA\Programs" -Force | Out-Null
        Expand-Archive -Path $zipPath -DestinationPath "$env:LOCALAPPDATA\Programs" -Force
        Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
        if (Test-Path "$ptDir\adb.exe") {
            $env:PATH = "$ptDir;$env:PATH"
            Write-Host "   ✔ adb installed in user space." -ForegroundColor Green
            return $true
        }
    } catch {
        Write-Host "   ⚠ Failed to install portable platform-tools: $_" -ForegroundColor DarkYellow
    }
    return $false
}

function Test-NodeCompatible {
    if (-not (Test-CommandExists "node") -or -not (Test-CommandExists "npm")) { return $false }
    try {
        $verRaw = (& node -v 2>$null)
        if (-not $verRaw) { return $false }
        $verStr = $verRaw.ToString().Trim().TrimStart('v')
        $parts = $verStr.Split('.')
        if ($parts.Count -ge 2) {
            $major = [int]$parts[0]
            $minor = [int]$parts[1]
            if ($major -ge 26) { return $true }
            if ($major -ge 24 -and $minor -ge 15) { return $true }
            if ($major -ge 22 -and $minor -ge 22) { return $true }
        }
    } catch {
        return $false
    }
    return $false
}

function Install-PortableNode {
    $nodeDir = "$env:LOCALAPPDATA\Programs\node"
    if (Test-Path "$nodeDir\node.exe") {
        $env:PATH = "$nodeDir;$env:PATH"
        if (Test-NodeCompatible) {
            return $true
        }
    }
    Write-Host "   [INFO] Installing portable Node.js LTS (v22.23.2) in user space..." -ForegroundColor Cyan
    try {
        $nodeVer = "v22.23.2"
        $arch = if ([System.Environment]::Is64BitOperatingSystem) {
            if ($env:PROCESSOR_ARCHITECTURE -match "ARM64") { "arm64" } else { "x64" }
        } else { "x64" }
        $zipPath = "$env:TEMP\node-$nodeVer-win-$arch.zip"
        Invoke-WebRequest -Uri "https://nodejs.org/dist/$nodeVer/node-$nodeVer-win-$arch.zip" -OutFile $zipPath -UseBasicParsing
        $extractDir = "$env:TEMP\node_extract"
        if (Test-Path $extractDir) { Remove-Item $extractDir -Recurse -Force -ErrorAction SilentlyContinue }
        New-Item -ItemType Directory -Path $extractDir -Force | Out-Null
        Expand-Archive -Path $zipPath -DestinationPath $extractDir -Force
        $extractedFolder = Get-ChildItem -Path $extractDir -Directory | Select-Object -First 1
        if ($extractedFolder) {
            New-Item -ItemType Directory -Path "$env:LOCALAPPDATA\Programs" -Force | Out-Null
            if (Test-Path $nodeDir) { Remove-Item $nodeDir -Recurse -Force -ErrorAction SilentlyContinue }
            Move-Item -Path $extractedFolder.FullName -Destination $nodeDir -Force
        }
        Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
        Remove-Item $extractDir -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path "$nodeDir\node.exe") {
            $env:PATH = "$nodeDir;$env:PATH"
            Write-Host "   ✔ Portable Node.js $nodeVer installed in user space." -ForegroundColor Green
            return $true
        }
    } catch {
        Write-Host "   ⚠ Failed to install portable Node.js: $_" -ForegroundColor DarkYellow
    }
    return $false
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
    Start-LocalAdbServer
} else {
    Write-Host "   ! Missing toolchains: $($missingTools -join ', ')" -ForegroundColor DarkYellow
    $useWinGet = $false
    if (Test-CommandExists "winget") {
        if ([Console]::IsInputRedirected -eq $false) {
            $ans = Read-Host "   Install system packages via WinGet? [Y/n]"
            if ($ans -eq "" -or $ans -match "^[Yy]") {
                $useWinGet = $true
            }
        } else {
            $useWinGet = $true
        }
    }

    if ($useWinGet) {
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
        Update-EnvironmentPath
    } elseif (Test-CommandExists "choco") {
        Write-Host "   Detected Chocolatey. Installing packages..." -ForegroundColor Cyan
        choco install ($missingTools -join " ") -y | Out-Null
        Update-EnvironmentPath
    } elseif (Test-CommandExists "scoop") {
        Write-Host "   Detected Scoop. Installing packages..." -ForegroundColor Cyan
        scoop install ($missingTools -join " ") | Out-Null
        Update-EnvironmentPath
    }

    # Zero-admin user-space fallback for adb if still missing
    if (-not (Test-CommandExists "adb")) {
        Install-PortablePlatformTools
    }

    # Ensure local ADB daemon is warm & listening
    Start-LocalAdbServer
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
    if (-not (Test-NodeCompatible)) {
        if (Test-CommandExists "node") {
            $curVer = (& node -v 2>$null)
            Write-Host "   ⚡ Detected Node.js $curVer, but Angular CLI requires Node.js >= v22.22.0." -ForegroundColor Yellow
        } else {
            Write-Host "   ⚡ Node.js/npm not found (required for Showcase UI)." -ForegroundColor Cyan
        }

        # 1. Try nvm if available on Windows
        if (Test-CommandExists "nvm") {
            Write-Host "   📦 Installing Node.js 22 LTS via nvm..." -ForegroundColor Cyan
            & nvm install 22.23.2 | Out-Null
            & nvm use 22.23.2 | Out-Null
            Update-EnvironmentPath
        }

        # 2. Try WinGet
        if (-not (Test-NodeCompatible)) {
            $useWinGetNode = $false
            if (Test-CommandExists "winget") {
                if ([Console]::IsInputRedirected -eq $false) {
                    $ans = Read-Host "   Install/Upgrade Node.js via WinGet (may require Administrator)? [Y/n]"
                    if ($ans -eq "" -or $ans -match "^[Yy]") {
                        $useWinGetNode = $true
                    }
                } else {
                    $useWinGetNode = $true
                }
            }

            if ($useWinGetNode) {
                Write-Host "   ⚡ Installing/Upgrading Node.js LTS via WinGet..." -ForegroundColor Cyan
                winget install --id OpenJS.NodeJS.LTS -e --accept-source-agreements --accept-package-agreements --silent 2>$null | Out-Null
                Update-EnvironmentPath
            }
        }

        # 3. Zero-admin user-space fallback for Node.js if still not compatible
        if (-not (Test-NodeCompatible)) {
            Install-PortableNode
        }
    }

    if (Test-NodeCompatible) {
        $nodeVer = (& node -v 2>$null)
        Write-Host "   ✔ Node.js $nodeVer is ready." -ForegroundColor Green
        Write-Host "   🎨 Compiling Angular Showcase UI..." -ForegroundColor Cyan
        Push-Location "$RootDir\apps\showcase_ui"
        try {
            npm install --silent
            $cliNodeVersion = "$RootDir\apps\showcase_ui\node_modules\@angular\cli\src\utilities\node-version.js"
            if (Test-Path $cliNodeVersion) {
                (Get-Content $cliNodeVersion) -replace '22\.22\.3', '22.22.0' | Set-Content $cliNodeVersion
            }
            npm run build
            Write-Host "   ✔ Showcase UI compiled successfully." -ForegroundColor Green
        } catch {
            Write-Host "   ⚠ Failed to build Showcase UI: $_" -ForegroundColor DarkYellow
        } finally {
            Pop-Location
        }
    } else {
        Write-Host "   ⚠ Could not configure compatible Node.js (>= 22.22.0). Showcase UI will show fallback notice on launch." -ForegroundColor DarkYellow
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
        if ($t -eq "npm" -and (Test-CommandExists "node")) {
            $nVer = (& node -v 2>$null)
            Write-Host "   ✔ $t ($nVer) -> $p" -ForegroundColor Green
        } else {
            Write-Host "   ✔ $t -> $p" -ForegroundColor Green
        }
    } else {
        Write-Host "   ○ $t -> Not found in PATH (Fallbacks active)" -ForegroundColor DarkYellow
    }
}

Write-Host ""
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host "   ✨ Artemis Environment Ready!                      " -ForegroundColor Green
Write-Host "======================================================" -ForegroundColor Cyan

if ($Launch -or $Open) {
    Start-LocalAdbServer
    Write-Host "🚀 Launching Showcase UI..." -ForegroundColor Green
    uv run artemis ui --open
} else {
    Write-Host "To start the Showcase UI and interactive onboarding:" -ForegroundColor White
    Write-Host "  👉 .\start.bat   (or: uv run artemis ui)" -ForegroundColor Cyan
    Write-Host ""
}

