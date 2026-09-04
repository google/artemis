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
            Write-Host "   [OK] adb installed in user space." -ForegroundColor Green
            return $true
        }
    } catch {
        Write-Host "   [WARN] Failed to install portable platform-tools: $_" -ForegroundColor DarkYellow
    }
    return $false
}

function Install-PortableNode {
    $nodeDir = "$env:LOCALAPPDATA\Programs\node"
    if (Test-Path "$nodeDir\node.exe") {
        $env:PATH = "$nodeDir;$env:PATH"
        return $true
    }
    Write-Host "   [INFO] Installing portable Node.js LTS in user space..." -ForegroundColor Cyan
    try {
        $zipPath = "$env:TEMP\node-v20-win-x64.zip"
        Invoke-WebRequest -Uri "https://nodejs.org/dist/v20.18.3/node-v20.18.3-win-x64.zip" -OutFile $zipPath -UseBasicParsing
        $extractDir = "$env:TEMP\node_extract"
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
            Write-Host "   [OK] Portable Node.js installed in user space." -ForegroundColor Green
            return $true
        }
    } catch {
        Write-Host "   [WARN] Failed to install portable Node.js: $_" -ForegroundColor DarkYellow
    }
    return $false
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
    $useWinGet = $false
    if (Test-CommandExists "winget") {
        if ([Console]::IsInputRedirected -eq $false) {
            Write-Host "   [INFO] Missing components detected: $($missingCore -join ', ')." -ForegroundColor Cyan
            $ans = Read-Host "      Install system packages via WinGet? [Y/n]"
            if ($ans -eq "" -or $ans -match "^[Yy]") {
                $useWinGet = $true
            }
        } else {
            $useWinGet = $true
        }
    }

    if ($useWinGet) {
        Write-Host "   [INFO] Auto-installing missing components ($($missingCore -join ', '))..." -ForegroundColor DarkYellow
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

    # Zero-admin user-space fallback for adb if still missing
    if (-not (Test-CommandExists "adb")) {
        Install-PortablePlatformTools
    }
}

# Ensure local ADB daemon is warm & listening so probes do not hit 'Connection refused'
Start-LocalAdbServer

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
        $useWinGetNode = $false
        if (Test-CommandExists "winget") {
            if ([Console]::IsInputRedirected -eq $false) {
                Write-Host "   [INFO] Node.js/npm not found (required for Showcase UI)." -ForegroundColor Cyan
                $ans = Read-Host "      Install Node.js via WinGet (may require Administrator)? [Y/n]"
                if ($ans -eq "" -or $ans -match "^[Yy]") {
                    $useWinGetNode = $true
                }
            } else {
                $useWinGetNode = $true
            }
        }

        if ($useWinGetNode) {
            Write-Host "   [INFO] Installing Node.js LTS via WinGet..." -ForegroundColor Yellow
            winget install --id OpenJS.NodeJS.LTS -e --accept-source-agreements --accept-package-agreements --silent 2>$null | Out-Null
            Update-EnvironmentPath
        }

        # Zero-admin user-space fallback for Node.js if still not found
        if (-not (Test-CommandExists "npm")) {
            Install-PortableNode
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
        Write-Host "   [WARN] Node.js/npm not found. Showcase UI will show fallback notice on launch." -ForegroundColor DarkYellow
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
# Ensure local ADB daemon is warm & listening
Start-LocalAdbServer

# Launch via `python -m artemis` (not the `artemis.exe` console-script shim) so the
# long-running server never locks .venv\Scripts\artemis.exe against `uv sync` reinstalls.
Write-Host "   [INFO] Launching Artemis Showcase UI & Admin Console..." -ForegroundColor Green
if ($NoOpen) {
    uv run python -m artemis ui --port $Port --no-open
} else {
    uv run python -m artemis ui --port $Port --open
}
