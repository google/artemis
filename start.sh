#!/usr/bin/env bash
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

set -euo pipefail

BOLD='\033[1m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

echo -e "${BOLD}${CYAN}======================================================${NC}"
echo -e "${BOLD}${CYAN}      ✨ Artemis Autonomous Mobile Agent UI          ${NC}"
echo -e "${BOLD}${CYAN}======================================================${NC}"
echo ""

# 1. Normalize PATH with standard toolchain and package manager paths
STANDARD_PATHS=(
    "/opt/homebrew/bin"
    "/opt/homebrew/sbin"
    "/usr/local/bin"
    "/usr/local/sbin"
    "${HOME}/.local/bin"
    "${HOME}/.local/share/node/bin"
    "${HOME}/.local/share/platform-tools"
    "${HOME}/.local/share/scrcpy"
    "${HOME}/.cargo/bin"
    "${HOME}/Library/Android/sdk/platform-tools"
    "${HOME}/Android/Sdk/platform-tools"
)
for p in "${STANDARD_PATHS[@]}"; do
    if [ -d "${p}" ] && [[ ":${PATH}:" != *":${p}:"* ]]; then
        export PATH="${p}:${PATH}"
    fi
done

# Sudo credential and choice state tracking
SUDO_AUTHENTICATED=false
SUDO_DECLINED=false

# Helper to check, prompt for, or skip administrator (sudo) privileges gracefully
request_sudo() {
    [ "$(id -u)" -eq 0 ] && return 0
    if ! command -v sudo >/dev/null 2>&1; then
        return 1
    fi
    # If already authenticated or passwordless sudo works
    if [ "${SUDO_AUTHENTICATED}" = true ] || sudo -n true >/dev/null 2>&1; then
        SUDO_AUTHENTICATED=true
        return 0
    fi
    # If previously declined or failed in this session, don't nag again
    if [ "${SUDO_DECLINED}" = true ]; then
        return 1
    fi
    # If interactive terminal, ask user whether to enter sudo password
    if [ -t 0 ]; then
        local action="${1:-install system packages}"
        echo -e "   ${CYAN}🔐 Administrator (sudo) privileges can be used to ${action}.${NC}"
        local reply=""
        read -r -p "      Enter sudo password now? [Y/n]: " reply
        reply=${reply:-Y}
        if [[ "${reply}" =~ ^[Yy]$ ]]; then
            if sudo -v; then
                SUDO_AUTHENTICATED=true
                echo -e "   ${GREEN}✔ Sudo authenticated successfully.${NC}"
                return 0
            else
                echo -e "   ${YELLOW}⚠ Sudo authentication failed or cancelled. Using user-space fallback.${NC}"
                SUDO_DECLINED=true
                return 1
            fi
        else
            echo -e "   ${YELLOW}⏭️  Skipped sudo. Using user-space fallback.${NC}"
            SUDO_DECLINED=true
            return 1
        fi
    fi
    return 1
}

# Helper to check if Node.js & npm meet Angular CLI 22 requirement (>= 22.22.0, >= 24.15.0, or >= 26.0.0)
is_node_compatible() {
    if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
        return 1
    fi
    local node_ver
    node_ver="$(node -v 2>/dev/null | tr -d 'v')"
    [ -z "${node_ver}" ] && return 1
    local major minor
    major="$(echo "${node_ver}" | cut -d. -f1)"
    minor="$(echo "${node_ver}" | cut -d. -f2)"
    if [ "${major}" -ge 26 ] 2>/dev/null; then
        return 0
    elif [ "${major}" -ge 24 ] 2>/dev/null; then
        [ "${minor}" -ge 15 ] 2>/dev/null && return 0
    elif [ "${major}" -ge 22 ] 2>/dev/null; then
        [ "${minor}" -ge 22 ] 2>/dev/null && return 0
    fi
    return 1
}

# 2. Check or install uv (Fast Python package manager)
if ! command -v uv >/dev/null 2>&1; then
    echo -e "${YELLOW}⚡ uv not found. Installing Astral uv...${NC}"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"
fi

# 3. Check and auto-install missing system toolchains (ADB, FFmpeg, scrcpy)
# Discover standard Android SDK / user-space locations if adb not in PATH
if ! command -v adb >/dev/null 2>&1; then
    for candidate in \
        "${ANDROID_HOME:-}/platform-tools" \
        "${ANDROID_SDK_ROOT:-}/platform-tools" \
        "${HOME}/Library/Android/sdk/platform-tools" \
        "${HOME}/Android/Sdk/platform-tools" \
        "${HOME}/.local/share/platform-tools" \
        "/opt/homebrew/bin" \
        "/usr/local/bin"; do
        if [ -n "${candidate}" ] && [ -x "${candidate}/adb" ]; then
            export PATH="${candidate}:${PATH}"
            break
        fi
    done
fi

MISSING_CORE=()
if ! command -v adb >/dev/null 2>&1; then MISSING_CORE+=("adb"); fi
if ! command -v ffmpeg >/dev/null 2>&1; then MISSING_CORE+=("ffmpeg"); fi
if ! command -v scrcpy >/dev/null 2>&1; then MISSING_CORE+=("scrcpy"); fi

if [ ${#MISSING_CORE[@]} -gt 0 ]; then
    OS_NAME="$(uname -s)"
    if [ "${OS_NAME}" = "Darwin" ]; then
        export HOMEBREW_NO_AUTO_UPDATE=1
        export HOMEBREW_NO_INSTALL_CLEANUP=1
        export HOMEBREW_NO_ENV_HINTS=1

        if ! command -v brew >/dev/null 2>&1; then
            if [ -x "/opt/homebrew/bin/brew" ]; then
                eval "$(/opt/homebrew/bin/brew shellenv)"
            elif [ -x "/usr/local/bin/brew" ]; then
                eval "$(/usr/local/bin/brew shellenv)"
            fi
        fi

        if command -v brew >/dev/null 2>&1; then
            echo -e "   ${YELLOW}⚡ Auto-installing missing components (${MISSING_CORE[*]})...${NC}"
            if ! command -v adb >/dev/null 2>&1; then
                brew install --cask android-platform-tools >/dev/null 2>&1 || brew install android-platform-tools >/dev/null 2>&1 || true
            fi
            if ! command -v ffmpeg >/dev/null 2>&1; then
                brew install ffmpeg >/dev/null 2>&1 || true
            fi
            if ! command -v scrcpy >/dev/null 2>&1; then
                brew install scrcpy >/dev/null 2>&1 || true
            fi
        fi
    elif [ "${OS_NAME}" = "Linux" ]; then
        if request_sudo "install missing system components (${MISSING_CORE[*]})"; then
            SUDO_PREFIX=""
            if [ "$(id -u)" -ne 0 ]; then SUDO_PREFIX="sudo"; fi
            if command -v apt-get >/dev/null 2>&1; then
                ${SUDO_PREFIX} apt-get -o DPkg::Lock::Timeout=60 update -qq 2>/dev/null || true
                ${SUDO_PREFIX} apt-get -o DPkg::Lock::Timeout=60 install -y -qq "${MISSING_CORE[@]}" 2>/dev/null || true
            elif command -v dnf >/dev/null 2>&1; then
                ${SUDO_PREFIX} dnf install -y "${MISSING_CORE[@]}" || true
            elif command -v pacman >/dev/null 2>&1; then
                ${SUDO_PREFIX} pacman -S --noconfirm "${MISSING_CORE[@]}" || true
            fi
        fi

        # Fallback: if scrcpy is still missing, try snap or install portable scrcpy in user space
        if ! command -v scrcpy >/dev/null 2>&1; then
            if command -v snap >/dev/null 2>&1 && request_sudo "install scrcpy via snap"; then
                local SUDO_PREFIX=""
                if [ "$(id -u)" -ne 0 ]; then SUDO_PREFIX="sudo"; fi
                ${SUDO_PREFIX} snap install scrcpy 2>/dev/null || true
            fi
        fi
        if ! command -v scrcpy >/dev/null 2>&1; then
            ARCH="$(uname -m)"
            SCRCPY_ARCH=""
            case "${ARCH}" in
                x86_64|amd64) SCRCPY_ARCH="x86_64" ;;
                aarch64|arm64) SCRCPY_ARCH="aarch64" ;;
            esac
            if [ -n "${SCRCPY_ARCH}" ]; then
                SCRCPY_DIR="${HOME}/.local/share/scrcpy"
                if [ ! -x "${SCRCPY_DIR}/scrcpy" ]; then
                    echo -e "   ${CYAN}📦 Installing portable scrcpy in user space (~/.local)...${NC}"
                    mkdir -p "${SCRCPY_DIR}" "${HOME}/.local/bin"
                    SCRCPY_URL="https://github.com/Genymobile/scrcpy/releases/download/v4.1/scrcpy-linux-${SCRCPY_ARCH}-v4.1.tar.gz"
                    if curl -fsSL "${SCRCPY_URL}" | tar -xz -C "${SCRCPY_DIR}" --strip-components=1 2>/dev/null; then
                        ln -sf "${SCRCPY_DIR}/scrcpy" "${HOME}/.local/bin/scrcpy"
                        export PATH="${SCRCPY_DIR}:${PATH}"
                        echo -e "   ${GREEN}✓ scrcpy installed in user space.${NC}"
                    fi
                else
                    ln -sf "${SCRCPY_DIR}/scrcpy" "${HOME}/.local/bin/scrcpy"
                    export PATH="${SCRCPY_DIR}:${PATH}"
                fi
            fi
        fi

        # Fallback: if adb is missing on Linux without root/sudo, install platform-tools in user space
        if ! command -v adb >/dev/null 2>&1; then
            ARCH="$(uname -m)"
            if [[ "${ARCH}" = "x86_64" || "${ARCH}" = "amd64" ]]; then
                PT_DIR="${HOME}/.local/share/platform-tools"
                if [ ! -x "${PT_DIR}/adb" ]; then
                    echo -e "   ${CYAN}📦 Installing Android platform-tools (adb) in user space...${NC}"
                    TEMP_ZIP="/tmp/platform-tools-$$.zip"
                    if curl -fsSL "https://dl.google.com/android/repository/platform-tools-latest-linux.zip" -o "${TEMP_ZIP}" 2>/dev/null; then
                        mkdir -p "${HOME}/.local/share" "${HOME}/.local/bin"
                        if command -v unzip >/dev/null 2>&1; then
                            unzip -q -o "${TEMP_ZIP}" -d "${HOME}/.local/share" 2>/dev/null || true
                        else
                            python3 -m zipfile -e "${TEMP_ZIP}" "${HOME}/.local/share" 2>/dev/null || true
                        fi
                        rm -f "${TEMP_ZIP}"
                    fi
                fi
                if [ -x "${PT_DIR}/adb" ]; then
                    ln -sf "${PT_DIR}/adb" "${HOME}/.local/bin/adb"
                    export PATH="${PT_DIR}:${PATH}"
                    echo -e "   ${GREEN}✓ adb installed in user space.${NC}"
                fi
            fi
        fi
    fi
fi

# Ensure local ADB daemon is warm & listening so probes do not hit 'Connection refused'
if command -v adb >/dev/null 2>&1; then
    (unset ADB_SERVER_SOCKET; adb start-server >/dev/null 2>&1 || true)
fi

# 4. Check or initialize .env configuration file
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo -e "   ${GREEN}✓ Initialized .env configuration file.${NC}"
    else
        touch .env
    fi
fi

# 5. Synchronize Python runtime & dependencies
echo -e "   ${BLUE}📦 Synchronizing Python runtime and project dependencies...${NC}"
uv sync --quiet

# 6. Check and build Showcase UI if not already compiled
SHOWCASE_INDEX="${SCRIPT_DIR}/apps/showcase_ui/dist/frontend/browser/index.html"
SHOWCASE_INDEX_ALT1="${SCRIPT_DIR}/apps/showcase_ui/dist/browser/index.html"
SHOWCASE_INDEX_ALT2="${SCRIPT_DIR}/apps/showcase_ui/dist/index.html"
if [ ! -f "${SHOWCASE_INDEX}" ] && [ ! -f "${SHOWCASE_INDEX_ALT1}" ] && [ ! -f "${SHOWCASE_INDEX_ALT2}" ]; then
    # First: Try loading existing nvm or user-installed node in environment
    export NVM_DIR="${HOME}/.nvm"
    if [ -s "${NVM_DIR}/nvm.sh" ]; then
        # shellcheck disable=SC1090,SC1091
        . "${NVM_DIR}/nvm.sh" 2>/dev/null || true
    fi
    if [ -d "${HOME}/.local/share/node/bin" ] && [[ ":${PATH}:" != *":${HOME}/.local/share/node/bin:"* ]]; then
        export PATH="${HOME}/.local/share/node/bin:${PATH}"
    fi

    # Check if nvm already has a compatible Node version installed
    if ! is_node_compatible && (command -v nvm >/dev/null 2>&1 || type nvm >/dev/null 2>&1); then
        nvm use 22 >/dev/null 2>&1 || true
    fi

    if ! is_node_compatible; then
        OS_NAME="$(uname -s)"
        if command -v node >/dev/null 2>&1; then
            echo -e "   ${YELLOW}⚡ Detected Node.js $(node -v 2>/dev/null), but Angular CLI requires Node.js >= v22.22.0. Upgrading Node.js...${NC}"
        else
            echo -e "   ${YELLOW}⚡ Node.js/npm not found. Auto-installing Node.js 22 LTS for Showcase UI compilation...${NC}"
        fi

        # 1. Try installing Node 22 via nvm if available
        if command -v nvm >/dev/null 2>&1 || type nvm >/dev/null 2>&1; then
            echo -e "   ${CYAN}📦 Installing Node.js 22 LTS via nvm...${NC}"
            nvm install 22 >/dev/null 2>&1 || true
            nvm use 22 >/dev/null 2>&1 || true
        fi

        # 2. Try brew on macOS
        if ! is_node_compatible && [ "${OS_NAME}" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
            brew install node >/dev/null 2>&1 || brew upgrade node >/dev/null 2>&1 || true
        fi

        # 3. Try Linux package managers with sudo / root
        if ! is_node_compatible && [ "${OS_NAME}" = "Linux" ]; then
            if request_sudo "install or upgrade Node.js to >= 22.22.0"; then
                SUDO_PREFIX=""
                if [ "$(id -u)" -ne 0 ]; then SUDO_PREFIX="sudo"; fi
                if command -v apt-get >/dev/null 2>&1; then
                    echo -e "   ${CYAN}📦 Configuring NodeSource Node.js 22 LTS repository...${NC}"
                    curl -fsSL https://deb.nodesource.com/setup_22.x | ${SUDO_PREFIX} bash - >/dev/null 2>&1 || true
                    ${SUDO_PREFIX} apt-get install -y -qq nodejs || true
                elif command -v dnf >/dev/null 2>&1; then
                    curl -fsSL https://rpm.nodesource.com/setup_22.x | ${SUDO_PREFIX} bash - >/dev/null 2>&1 || true
                    ${SUDO_PREFIX} dnf install -y nodejs || true
                elif command -v pacman >/dev/null 2>&1; then
                    ${SUDO_PREFIX} pacman -S --noconfirm nodejs npm || true
                fi
            fi
        fi

        # 4. Standalone portable Node.js LTS (zero-admin user space fallback, guaranteed compatible)
        if ! is_node_compatible; then
            ARCH="$(uname -m)"
            NODE_ARCH=""
            case "${ARCH}" in
                x86_64|amd64) NODE_ARCH="x64" ;;
                aarch64|arm64) NODE_ARCH="arm64" ;;
            esac
            OS_SYS="$(uname -s | tr '[:upper:]' '[:lower:]')"
            if [ -n "${NODE_ARCH}" ] && { [ "${OS_SYS}" = "linux" ] || [ "${OS_SYS}" = "darwin" ]; }; then
                NODE_VER="v22.23.2"
                NODE_DIR="${HOME}/.local/share/node"
                echo -e "   ${CYAN}📦 Installing portable Node.js ${NODE_VER} in user space (~/.local)...${NC}"
                rm -rf "${NODE_DIR}"
                mkdir -p "${NODE_DIR}" "${HOME}/.local/bin"
                if curl -fsSL "https://nodejs.org/dist/${NODE_VER}/node-${NODE_VER}-${OS_SYS}-${NODE_ARCH}.tar.gz" | tar -xz -C "${NODE_DIR}" --strip-components=1 2>/dev/null; then
                    ln -sf "${NODE_DIR}/bin/node" "${HOME}/.local/bin/node"
                    ln -sf "${NODE_DIR}/bin/npm" "${HOME}/.local/bin/npm"
                    ln -sf "${NODE_DIR}/bin/npx" "${HOME}/.local/bin/npx"
                    export PATH="${NODE_DIR}/bin:${HOME}/.local/bin:${PATH}"
                    hash -r 2>/dev/null || true
                    echo -e "   ${GREEN}✓ Portable Node.js ${NODE_VER} installed in user space.${NC}"
                fi
            fi
        fi
    fi

    if is_node_compatible; then
        echo -e "   ${GREEN}✓ Node.js $(node -v 2>/dev/null) and npm $(npm -v 2>/dev/null) ready.${NC}"
        echo -e "   ${YELLOW}🎨 Showcase UI build not found. Compiling Angular Showcase UI...${NC}"
        (
            cd "${SCRIPT_DIR}/apps/showcase_ui"
            npm install --silent
            # Auto-patch Angular CLI node version constraint if running on Node 22.22.x (e.g. Cloudtop / Debian)
            CLI_NODE_VERSION="${SCRIPT_DIR}/apps/showcase_ui/node_modules/@angular/cli/src/utilities/node-version.js"
            if [ -f "${CLI_NODE_VERSION}" ]; then
                if [ "$(uname -s)" = "Darwin" ]; then
                    sed -i '' 's/22\.22\.3/22.22.0/g' "${CLI_NODE_VERSION}" 2>/dev/null || true
                else
                    sed -i 's/22\.22\.3/22.22.0/g' "${CLI_NODE_VERSION}" 2>/dev/null || true
                fi
            fi
            npm run build
        )
    else
        echo -e "   ${YELLOW}⚠ Could not configure compatible Node.js (>= 22.22.0). Showcase UI will serve fallback build notice.${NC}"
    fi
fi

# 7. Optionally configure MCP for detected AI IDEs
echo ""
echo -e "   ${CYAN}🔌 Would you like to configure ARTEMIS MCP & testing rules for your AI IDEs?${NC}"
echo -e "      (Supported: Antigravity, Cursor, Claude Code/Desktop, Codex, OpenClaw, Windsurf)"
if [ -t 0 ]; then
    read -r -p "      Install MCP configuration & rules now? [Y/n]: " INSTALL_MCP
    INSTALL_MCP=${INSTALL_MCP:-Y}
else
    INSTALL_MCP="n"
fi

if [[ "${INSTALL_MCP}" =~ ^[Yy]$ ]]; then
    echo -e "   ${CYAN}Installing MCP server configuration & testing rules...${NC}"
    if uv run artemis mcp --install all; then
        echo -e "   ${GREEN}✔ MCP configuration and rules installed successfully.${NC}"
        echo -e "   ${CYAN}💡 Tip: You can update or re-install anytime with: ${BOLD}uv run artemis mcp --install all${NC}"
    else
        echo -e "   ${YELLOW}⚠ MCP installation failed. Review the error above and retry with: ${BOLD}uv run artemis mcp --install all${NC}"
    fi
else
    echo -e "   ${YELLOW}⏭️  Skipped. You can install MCP anytime later with: ${BOLD}uv run artemis mcp --install all${NC}"
fi
echo ""

# 8. Detect environment & launch unified Showcase UI
IS_REMOTE=false
if [ -n "${SSH_CONNECTION:-}" ] || [ -n "${SSH_CLIENT:-}" ] || [ -n "${SSH_TTY:-}" ] || [ -z "${DISPLAY:-}" ]; then
    IS_REMOTE=true
fi

echo -e "   ${GREEN}🚀 Launching Artemis Showcase UI & Admin Console...${NC}"

OPEN_FLAG="--open"
if [ "${IS_REMOTE}" = true ]; then
    HOSTNAME_STR="$(hostname 2>/dev/null || echo 'cloud-host')"
    USER_STR="$(whoami 2>/dev/null || echo 'user')"
    echo -e "   ${CYAN}☁️  Cloud / Remote environment detected:${NC}"
    echo -e "      • Access locally via SSH tunnel: ${BOLD}ssh -L 8000:localhost:8000 ${USER_STR}@${HOSTNAME_STR}${NC}"
    echo -e "      • Or access via Cloudtop / VS Code / Cursor Port Forwarding (Port 8000)"
    if [ -z "${DISPLAY:-}" ]; then
        echo -e "      • Headless session detected (browser auto-open disabled)."
        OPEN_FLAG="--no-open"
    fi
    echo ""
fi

# Ensure local ADB daemon is active and listening before launching UI
if command -v adb >/dev/null 2>&1; then
    (unset ADB_SERVER_SOCKET; adb start-server >/dev/null 2>&1 || true)
fi

# Launch via `python -m artemis` (not the `artemis` console-script shim) so the
# long-running server never pins .venv/Scripts/artemis[.exe] against reinstalls.
exec uv run python -m artemis ui "${OPEN_FLAG}" "$@"
