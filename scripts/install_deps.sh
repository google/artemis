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

echo -e "${BOLD}1. Detecting Operating System & Environment...${NC}"
echo -e "   Platform: ${BLUE}${OS_TYPE}${NC} (${ARCH_TYPE})"
echo -e "   Root Dir: ${DIM}${ROOT_DIR}${NC}"

# Function to install system toolchains (ADB, FFmpeg, scrcpy)
install_system_packages() {
    echo -e "\n${BOLD}2. Checking System Toolchains (ADB, FFmpeg, scrcpy)...${NC}"

    local need_adb=false
    local need_ffmpeg=false
    local need_scrcpy=false

    if ! has_cmd adb; then need_adb=true; fi
    if ! has_cmd ffmpeg; then need_ffmpeg=true; fi
    if ! has_cmd scrcpy; then need_scrcpy=true; fi

    if [ "${need_adb}" = false ] && [ "${need_ffmpeg}" = false ] && [ "${need_scrcpy}" = false ]; then
        echo -e "   ${GREEN}✓ All core system tools are already installed.${NC}"
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
        SUDO_CMD=""
        if [ "$(id -u)" -ne 0 ]; then
            if has_cmd sudo; then
                SUDO_CMD="sudo"
            else
                echo -e "   ${YELLOW}⚠ Root/sudo privileges not detected. Please install system tools with your package manager.${NC}"
            fi
        fi

        if has_cmd apt-get; then
            echo -e "   ${CYAN}Detected Debian/Ubuntu (apt-get). Synchronizing and installing...${NC}"
            PKGS=()
            [ "${need_adb}" = true ] && PKGS+=("adb")
            [ "${need_ffmpeg}" = true ] && PKGS+=("ffmpeg")
            [ "${need_scrcpy}" = true ] && PKGS+=("scrcpy")
            PKGS+=("curl" "git")

            if [ -n "${SUDO_CMD}" ] || [ "$(id -u)" -eq 0 ]; then
                ${SUDO_CMD} apt-get update -qq || true
                ${SUDO_CMD} apt-get install -y "${PKGS[@]}" || true
            fi
        elif has_cmd dnf; then
            echo -e "   ${CYAN}Detected Fedora/RHEL (dnf). Installing packages...${NC}"
            PKGS=()
            [ "${need_adb}" = true ] && PKGS+=("android-tools")
            [ "${need_ffmpeg}" = true ] && PKGS+=("ffmpeg")
            [ "${need_scrcpy}" = true ] && PKGS+=("scrcpy")
            PKGS+=("curl" "git")

            if [ -n "${SUDO_CMD}" ] || [ "$(id -u)" -eq 0 ]; then
                ${SUDO_CMD} dnf install -y "${PKGS[@]}" || true
            fi
        elif has_cmd pacman; then
            echo -e "   ${CYAN}Detected Arch Linux (pacman). Installing packages...${NC}"
            PKGS=()
            [ "${need_adb}" = true ] && PKGS+=("android-tools")
            [ "${need_ffmpeg}" = true ] && PKGS+=("ffmpeg")
            [ "${need_scrcpy}" = true ] && PKGS+=("scrcpy")
            PKGS+=("curl" "git")

            if [ -n "${SUDO_CMD}" ] || [ "$(id -u)" -eq 0 ]; then
                ${SUDO_CMD} pacman -S --noconfirm "${PKGS[@]}" || true
            fi
        fi
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
        if ! has_cmd npm; then
            echo -e "   ${YELLOW}⚡ npm/Node.js not found. Auto-installing Node.js...${NC}"
            if [ "${OS_TYPE}" = "Darwin" ] && has_cmd brew; then
                brew install node >/dev/null 2>&1 || true
            elif [ "${OS_TYPE}" = "Linux" ]; then
                local SUDO_CMD=""
                if [ "$(id -u)" -ne 0 ] && has_cmd sudo; then SUDO_CMD="sudo"; fi
                if has_cmd apt-get; then
                    ${SUDO_CMD} apt-get update -qq && ${SUDO_CMD} apt-get install -y -qq nodejs npm >/dev/null 2>&1 || true
                elif has_cmd dnf; then
                    ${SUDO_CMD} dnf install -y nodejs npm >/dev/null 2>&1 || true
                elif has_cmd pacman; then
                    ${SUDO_CMD} pacman -S --noconfirm nodejs npm >/dev/null 2>&1 || true
                fi
            fi
        fi

        # Try loading nvm if available in user environment
        export NVM_DIR="${HOME}/.nvm"
        if [ -s "${NVM_DIR}/nvm.sh" ]; then
            # shellcheck disable=SC1090,SC1091
            . "${NVM_DIR}/nvm.sh" 2>/dev/null || true
        fi

        if has_cmd npm; then
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
            echo -e "   ${YELLOW}⚠ Could not find npm. Showcase UI will show fallback notice on launch.${NC}"
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
            echo -e "   ${GREEN}✔ ${t}${NC} -> ${DIM}${loc}${NC}"
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
