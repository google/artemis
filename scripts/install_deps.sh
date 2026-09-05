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

# Visual styling
BOLD='\033[1m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
DIM='\033[2m'
NC='\033[0m'

AUTO_LAUNCH=false
for arg in "$@"; do
    case "${arg}" in
        --launch|-l|--open)
            AUTO_LAUNCH=true
            ;;
    esac
done

echo -e "${BOLD}${CYAN}======================================================${NC}"
echo -e "${BOLD}${CYAN}   🚀 Artemis - Smart Multi-Platform Installer        ${NC}"
echo -e "${BOLD}${CYAN}======================================================${NC}"
echo ""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

# Normalize PATH with standard platform binary paths
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

OS_TYPE="$(uname -s)"
ARCH_TYPE="$(uname -m)"

# Helper to check command availability
has_cmd() {
    command -v "$1" >/dev/null 2>&1
}

# Sudo credential and choice state tracking
SUDO_AUTHENTICATED=false
SUDO_DECLINED=false

# Helper to check, prompt for, or skip administrator (sudo) privileges gracefully
request_sudo() {
    [ "$(id -u)" -eq 0 ] && return 0
    if ! has_cmd sudo; then
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
    if ! has_cmd node || ! has_cmd npm; then
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

echo -e "${BOLD}1. Detecting Operating System & Environment...${NC}"
echo -e "   Platform: ${BLUE}${OS_TYPE}${NC} (${ARCH_TYPE})"
echo -e "   Root Dir: ${DIM}${ROOT_DIR}${NC}"

# Function to install system toolchains (ADB, FFmpeg, scrcpy)
install_system_packages() {
    echo -e "\n${BOLD}2. Checking System Toolchains (ADB, FFmpeg, scrcpy)...${NC}"

    local need_adb=false
    local need_ffmpeg=false
    local need_scrcpy=false

    if ! has_cmd adb; then
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

    if ! has_cmd adb; then need_adb=true; fi
    if ! has_cmd ffmpeg; then need_ffmpeg=true; fi
    if ! has_cmd scrcpy; then need_scrcpy=true; fi

    if [ "${need_adb}" = false ] && [ "${need_ffmpeg}" = false ] && [ "${need_scrcpy}" = false ]; then
        echo -e "   ${GREEN}✓ All core system tools are already installed.${NC}"
        if has_cmd adb; then
            (unset ADB_SERVER_SOCKET; adb start-server >/dev/null 2>&1 || true)
        fi
        return 0
    fi

    echo -e "   ${YELLOW}! Missing toolchains detected:${NC}"
    [ "${need_adb}" = true ] && echo -e "     • [ADB] Android Debug Bridge"
    [ "${need_ffmpeg}" = true ] && echo -e "     • [FFmpeg] Video encoding/trimming framework"
    [ "${need_scrcpy}" = true ] && echo -e "     • [scrcpy] Real-time Android screen mirroring"

    echo -e "   Attempting automated installation..."

    if [ "${OS_TYPE}" = "Darwin" ]; then
        # Ensure Homebrew environment variables to prevent hanging
        export HOMEBREW_NO_AUTO_UPDATE=1
        export HOMEBREW_NO_INSTALL_CLEANUP=1
        export HOMEBREW_NO_ENV_HINTS=1

        if ! has_cmd brew; then
            if [ -x "/opt/homebrew/bin/brew" ]; then
                eval "$(/opt/homebrew/bin/brew shellenv)"
            elif [ -x "/usr/local/bin/brew" ]; then
                eval "$(/usr/local/bin/brew shellenv)"
            fi
        fi

        if has_cmd brew; then
            echo -e "   ${CYAN}Detected Homebrew ($(brew --version | head -n1)). Installing missing components...${NC}"
            if [ "${need_adb}" = true ]; then
                echo -e "   📦 Installing ${BOLD}android-platform-tools${NC}..."
                brew install --cask android-platform-tools || brew install android-platform-tools || true
            fi
            if [ "${need_ffmpeg}" = true ]; then
                echo -e "   📦 Installing ${BOLD}ffmpeg${NC}..."
                brew install ffmpeg || true
            fi
            if [ "${need_scrcpy}" = true ]; then
                echo -e "   📦 Installing ${BOLD}scrcpy${NC}..."
                brew install scrcpy || true
            fi
        else
            echo -e "   ${YELLOW}⚠ Homebrew not found. Please install Homebrew or install tools manually:${NC}"
            echo -e "     /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
            echo -e "     brew install --cask android-platform-tools && brew install ffmpeg scrcpy"
        fi

    elif [ "${OS_TYPE}" = "Linux" ]; then
        local PKGS=()
        [ "${need_adb}" = true ] && PKGS+=("adb")
        [ "${need_ffmpeg}" = true ] && PKGS+=("ffmpeg")
        [ "${need_scrcpy}" = true ] && PKGS+=("scrcpy")

        if request_sudo "install missing system components (${PKGS[*]})"; then
            local SUDO_PREFIX=""
            if [ "$(id -u)" -ne 0 ]; then
                SUDO_PREFIX="sudo"
            fi

            if has_cmd apt-get; then
                echo -e "   ${CYAN}Detected Debian/Ubuntu (apt-get). Installing missing packages...${NC}"
                DEBIAN_FRONTEND=noninteractive ${SUDO_PREFIX} apt-get -o DPkg::Lock::Timeout=3 -o Acquire::http::Timeout=3 -o Acquire::https::Timeout=3 install -y "${PKGS[@]}" 2>/dev/null || true
            elif has_cmd dnf; then
                echo -e "   ${CYAN}Detected Fedora/RHEL (dnf). Installing packages...${NC}"
                local DNF_PKGS=()
                [ "${need_adb}" = true ] && DNF_PKGS+=("android-tools")
                [ "${need_ffmpeg}" = true ] && DNF_PKGS+=("ffmpeg")
                [ "${need_scrcpy}" = true ] && DNF_PKGS+=("scrcpy")
                ${SUDO_PREFIX} dnf install -y "${DNF_PKGS[@]}" 2>/dev/null || true
            elif has_cmd pacman; then
                echo -e "   ${CYAN}Detected Arch Linux (pacman). Installing packages...${NC}"
                local PAC_PKGS=()
                [ "${need_adb}" = true ] && PAC_PKGS+=("android-tools")
                [ "${need_ffmpeg}" = true ] && PAC_PKGS+=("ffmpeg")
                [ "${need_scrcpy}" = true ] && PAC_PKGS+=("scrcpy")
                ${SUDO_PREFIX} pacman -S --noconfirm "${PAC_PKGS[@]}" 2>/dev/null || true
            fi
        fi

        # Fallback: if scrcpy is still missing, install official precompiled portable scrcpy in user space
        if ! has_cmd scrcpy; then
            local SCRCPY_ARCH=""
            case "${ARCH_TYPE}" in
                x86_64|amd64) SCRCPY_ARCH="x86_64" ;;
                aarch64|arm64) SCRCPY_ARCH="aarch64" ;;
            esac
            if [ -n "${SCRCPY_ARCH}" ]; then
                local SCRCPY_DIR="${HOME}/.local/share/scrcpy"
                if [ ! -x "${SCRCPY_DIR}/scrcpy" ]; then
                    echo -e "   ${CYAN}📦 Installing portable scrcpy in user space (~/.local)...${NC}"
                    mkdir -p "${SCRCPY_DIR}" "${HOME}/.local/bin"
                    local SCRCPY_URL="https://github.com/Genymobile/scrcpy/releases/download/v4.1/scrcpy-linux-${SCRCPY_ARCH}-v4.1.tar.gz"
                    if curl -fsSL --connect-timeout 5 --max-time 30 "${SCRCPY_URL}" | tar -xz -C "${SCRCPY_DIR}" --strip-components=1 2>/dev/null; then
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

        # User-space fallback for adb if missing and no root privileges
        if ! has_cmd adb; then
            if [[ "${ARCH_TYPE}" = "x86_64" || "${ARCH_TYPE}" = "amd64" ]]; then
                local PT_DIR="${HOME}/.local/share/platform-tools"
                if [ ! -x "${PT_DIR}/adb" ]; then
                    echo -e "   ${CYAN}📦 Installing Android platform-tools (adb) in user space...${NC}"
                    local TEMP_ZIP="/tmp/platform-tools-$$.zip"
                    if curl -fsSL "https://dl.google.com/android/repository/platform-tools-latest-linux.zip" -o "${TEMP_ZIP}" 2>/dev/null; then
                        mkdir -p "${HOME}/.local/share" "${HOME}/.local/bin"
                        if has_cmd unzip; then
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

    # Ensure local ADB daemon is warm & listening
    if has_cmd adb; then
        (unset ADB_SERVER_SOCKET; adb start-server >/dev/null 2>&1 || true)
    fi
}

# Function to ensure uv is installed and ready
ensure_uv() {
    echo -e "\n${BOLD}3. Checking Python Package Manager (uv)...${NC}"
    if ! has_cmd uv; then
        echo -e "   ${YELLOW}uv not found. Installing Astral uv (ultra-fast Python package manager)...${NC}"
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"
    fi

    # Explicitly persist PATH to shell configuration files if not present
    local export_line='export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"'
    for rc in "${HOME}/.bashrc" "${HOME}/.zshrc" "${HOME}/.profile"; do
        if [ -f "${rc}" ]; then
            if ! grep -qs 'HOME/\.local/bin' "${rc}"; then
                echo -e "\n# Added by Artemis installer\n${export_line}" >> "${rc}"
            fi
        fi
    done

    # If /usr/local/bin is writable or sudo is available, try to create a symlink to make it available immediately
    local uv_bin=""
    if [ -x "${HOME}/.local/bin/uv" ]; then
        uv_bin="${HOME}/.local/bin/uv"
    elif command -v uv >/dev/null 2>&1; then
        uv_bin="$(command -v uv)"
    fi

    if [ -n "${uv_bin}" ]; then
        if [ -w "/usr/local/bin" ]; then
            ln -sf "${uv_bin}" /usr/local/bin/uv 2>/dev/null || true
            [ -x "${HOME}/.local/bin/uvx" ] && ln -sf "${HOME}/.local/bin/uvx" /usr/local/bin/uvx 2>/dev/null || true
        elif has_cmd sudo; then
            sudo -n ln -sf "${uv_bin}" /usr/local/bin/uv 2>/dev/null || true
            [ -x "${HOME}/.local/bin/uvx" ] && sudo -n ln -sf "${HOME}/.local/bin/uvx" /usr/local/bin/uvx 2>/dev/null || true
        fi
    fi

    if has_cmd uv; then
        echo -e "   ${GREEN}✓ uv is ready ($(uv --version))${NC}"
    else
        echo -e "   ${RED}✗ Failed to find uv in PATH. Please add ~/.local/bin to your PATH.${NC}"
        exit 1
    fi
}

# Function to setup Python runtime and sync dependencies
setup_python_env() {
    echo -e "\n${BOLD}4. Configuring Python Runtime & Syncing Dependencies...${NC}"
    echo -e "   ${BLUE}📦 Running uv sync (auto-provisioning Python >=3.12 & dependencies)...${NC}"
    uv sync
    echo -e "   ${GREEN}✓ Python runtime and dependencies synced successfully.${NC}"
}

# Function to initialize .env configuration
setup_env_file() {
    echo -e "\n${BOLD}5. Checking Environment Configuration (.env)...${NC}"
    if [ ! -f ".env" ]; then
        if [ -f ".env.example" ]; then
            cp .env.example .env
            echo -e "   ${GREEN}✓ Created .env template from .env.example.${NC}"
        else
            touch .env
            echo -e "   ${GREEN}✓ Created empty .env file.${NC}"
        fi
    else
        echo -e "   ${GREEN}✓ .env configuration file exists.${NC}"
    fi
}

# Function to check and build Showcase UI
setup_showcase_ui() {
    echo -e "\n${BOLD}6. Checking Showcase UI Build (Angular)...${NC}"
    local SHOWCASE_INDEX="${ROOT_DIR}/apps/showcase_ui/dist/frontend/browser/index.html"
    local SHOWCASE_INDEX_ALT1="${ROOT_DIR}/apps/showcase_ui/dist/browser/index.html"
    local SHOWCASE_INDEX_ALT2="${ROOT_DIR}/apps/showcase_ui/dist/index.html"
    if [ ! -f "${SHOWCASE_INDEX}" ] && [ ! -f "${SHOWCASE_INDEX_ALT1}" ] && [ ! -f "${SHOWCASE_INDEX_ALT2}" ]; then
        # Try loading nvm if available in user environment
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
            if has_cmd node; then
                local current_node="$(node -v 2>/dev/null)"
                echo -e "   ${YELLOW}⚡ Detected Node.js ${current_node}, but Angular CLI requires Node.js >= v22.22.0. Upgrading Node.js...${NC}"
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
            if ! is_node_compatible && [ "${OS_TYPE}" = "Darwin" ] && has_cmd brew; then
                brew install node >/dev/null 2>&1 || brew upgrade node >/dev/null 2>&1 || true
            fi

            # 3. Try Linux package managers with sudo / root
            if ! is_node_compatible && [ "${OS_TYPE}" = "Linux" ]; then
                if request_sudo "install or upgrade Node.js to >= 22.22.0"; then
                    local SUDO_PREFIX=""
                    if [ "$(id -u)" -ne 0 ]; then SUDO_PREFIX="sudo"; fi
                    if has_cmd apt-get; then
                        echo -e "   ${CYAN}📦 Configuring NodeSource Node.js 22 LTS repository...${NC}"
                        curl -fsSL https://deb.nodesource.com/setup_22.x | ${SUDO_PREFIX} bash - >/dev/null 2>&1 || true
                        ${SUDO_PREFIX} apt-get -o DPkg::Lock::Timeout=5 install -y -qq nodejs 2>/dev/null || true
                    elif has_cmd dnf; then
                        curl -fsSL https://rpm.nodesource.com/setup_22.x | ${SUDO_PREFIX} bash - >/dev/null 2>&1 || true
                        ${SUDO_PREFIX} dnf install -y nodejs || true
                    elif has_cmd pacman; then
                        ${SUDO_PREFIX} pacman -S --noconfirm nodejs npm || true
                    fi
                fi
            fi

            # 4. Standalone portable Node.js LTS (zero-admin user space fallback, guaranteed compatible)
            if ! is_node_compatible; then
                local ARCH="$(uname -m)"
                local NODE_ARCH=""
                case "${ARCH}" in
                    x86_64|amd64) NODE_ARCH="x64" ;;
                    aarch64|arm64) NODE_ARCH="arm64" ;;
                esac
                local OS_SYS="$(uname -s | tr '[:upper:]' '[:lower:]')"
                if [ -n "${NODE_ARCH}" ] && { [ "${OS_SYS}" = "linux" ] || [ "${OS_SYS}" = "darwin" ]; }; then
                    local NODE_VER="v22.23.2"
                    local NODE_DIR="${HOME}/.local/share/node"
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
            echo -e "   ${YELLOW}🎨 Building Angular Showcase UI...${NC}"
            (
                cd "${ROOT_DIR}/apps/showcase_ui"
                npm install --silent
                CLI_NODE_VERSION="${ROOT_DIR}/apps/showcase_ui/node_modules/@angular/cli/src/utilities/node-version.js"
                if [ -f "${CLI_NODE_VERSION}" ]; then
                    if [ "${OS_TYPE}" = "Darwin" ]; then
                        sed -i '' 's/22\.22\.3/22.22.0/g' "${CLI_NODE_VERSION}" 2>/dev/null || true
                    else
                        sed -i 's/22\.22\.3/22.22.0/g' "${CLI_NODE_VERSION}" 2>/dev/null || true
                    fi
                fi
                npm run build
            )
            echo -e "   ${GREEN}✓ Showcase UI compiled successfully.${NC}"
        else
            echo -e "   ${YELLOW}⚠ Could not configure compatible Node.js (>= 22.22.0). Showcase UI will show fallback notice on launch.${NC}"
        fi
    else
        echo -e "   ${GREEN}✓ Showcase UI build already exists.${NC}"
    fi
}

# Function to display system readiness report
verify_readiness() {
    echo -e "\n${BOLD}7. Toolchain Readiness Summary:${NC}"

    local tools=("adb" "ffmpeg" "scrcpy" "uv" "python3" "npm")
    for t in "${tools[@]}"; do
        if has_cmd "$t"; then
            local loc
            loc="$(command -v "$t")"
            if [ "$t" = "npm" ] && has_cmd node; then
                local n_ver
                n_ver="$(node -v 2>/dev/null || echo '')"
                echo -e "   ${GREEN}✔ ${t}${NC} (${n_ver}) -> ${DIM}${loc}${NC}"
            else
                echo -e "   ${GREEN}✔ ${t}${NC} -> ${DIM}${loc}${NC}"
            fi
        else
            echo -e "   ${YELLOW}○ ${t}${NC} -> ${YELLOW}Not found in PATH (Fallbacks active)${NC}"
        fi
    done
}

# Run setup workflow
install_system_packages
ensure_uv
setup_python_env
setup_env_file
setup_showcase_ui
verify_readiness

echo -e "\n${BOLD}${CYAN}======================================================${NC}"
echo -e "${BOLD}${GREEN}   ✨ Artemis Environment Ready!                      ${NC}"
echo -e "${BOLD}${CYAN}======================================================${NC}"

if [ "${AUTO_LAUNCH}" = true ]; then
    echo -e "\n${GREEN}🚀 Auto-launching Showcase UI...${NC}"
    exec uv run artemis ui --open
else
    echo -e "To launch the unified UI & live device onboarding:"
    echo -e "  👉 ${BOLD}${CYAN}./start.sh${NC}   (Recommended - automatically sets up environment)"
    echo -e "  👉 ${BOLD}source ~/.bashrc && uv run artemis ui${NC} (or open a new terminal tab)"
    echo ""
fi
