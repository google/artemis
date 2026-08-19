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
    "${HOME}/.cargo/bin"
    "${HOME}/Library/Android/sdk/platform-tools"
    "${HOME}/Android/Sdk/platform-tools"
)
for p in "${STANDARD_PATHS[@]}"; do
    if [ -d "${p}" ] && [[ ":${PATH}:" != *":${p}:"* ]]; then
        export PATH="${p}:${PATH}"
    fi
done

# 2. Check or install uv (Fast Python package manager)
if ! command -v uv >/dev/null 2>&1; then
    echo -e "${YELLOW}⚡ uv not found. Installing Astral uv...${NC}"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"
fi

# 3. Check and auto-install missing system toolchains (ADB, FFmpeg, scrcpy)
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
        if command -v apt-get >/dev/null 2>&1; then
            if [ "$(id -u)" -eq 0 ]; then
                apt-get update -qq && apt-get install -y -qq "${MISSING_CORE[@]}" >/dev/null 2>&1 || true
            elif command -v sudo >/dev/null 2>&1; then
                sudo -n apt-get update -qq && sudo -n apt-get install -y -qq "${MISSING_CORE[@]}" >/dev/null 2>&1 || true
            fi
        fi
    fi
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
    if ! command -v npm >/dev/null 2>&1; then
        OS_NAME="$(uname -s)"
        echo -e "   ${YELLOW}⚡ npm/Node.js not found. Auto-installing Node.js for Showcase UI compilation...${NC}"
        if [ "${OS_NAME}" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
            brew install node >/dev/null 2>&1 || true
        elif [ "${OS_NAME}" = "Linux" ]; then
            if command -v apt-get >/dev/null 2>&1; then
                if [ "$(id -u)" -eq 0 ]; then
                    apt-get update -qq && apt-get install -y -qq nodejs npm >/dev/null 2>&1 || true
                elif command -v sudo >/dev/null 2>&1; then
                    sudo -n apt-get update -qq && sudo -n apt-get install -y -qq nodejs npm >/dev/null 2>&1 || true
                fi
            elif command -v dnf >/dev/null 2>&1; then
                if [ "$(id -u)" -eq 0 ]; then
                    dnf install -y nodejs npm >/dev/null 2>&1 || true
                elif command -v sudo >/dev/null 2>&1; then
                    sudo -n dnf install -y nodejs npm >/dev/null 2>&1 || true
                fi
            elif command -v pacman >/dev/null 2>&1; then
                if [ "$(id -u)" -eq 0 ]; then
                    pacman -S --noconfirm nodejs npm >/dev/null 2>&1 || true
                elif command -v sudo >/dev/null 2>&1; then
                    sudo -n pacman -S --noconfirm nodejs npm >/dev/null 2>&1 || true
                fi
            fi
        fi
    fi

    if command -v npm >/dev/null 2>&1; then
        echo -e "   ${YELLOW}🎨 Showcase UI build not found. Compiling Angular Showcase UI...${NC}"
        (cd "${SCRIPT_DIR}/apps/showcase_ui" && npm install --silent && npm run build)
    else
        echo -e "   ${YELLOW}⚠ Could not auto-install Node.js. Showcase UI will serve fallback build notice.${NC}"
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

# 8. Launch unified Showcase UI & auto-open browser
echo -e "   ${GREEN}🚀 Launching Artemis Showcase UI & Admin Console...${NC}"
exec uv run artemis ui --open "$@"
