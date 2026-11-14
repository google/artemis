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

# Read-only prerequisite checks. This script does not install tools, boot
# devices, select Xcode, or change Xcode's MCP permissions.
set -euo pipefail

fail() {
    printf 'Error: %s\n' "$1" >&2
    exit 1
}

[[ "$(uname -s)" == "Darwin" ]] || fail "iOS support requires macOS."

command -v xcodebuild >/dev/null 2>&1 || fail "Install Xcode 27 or newer."
command -v xcrun >/dev/null 2>&1 || fail "Xcode's xcrun is unavailable."

if ! version_output=$(xcodebuild -version); then
    fail "Cannot read the selected Xcode version. Check xcode-select -p."
fi
version=$(printf '%s\n' "$version_output" | awk '/^Xcode / {print $2; exit}')
[[ "$version" =~ ^([0-9]+)(\.[0-9]+)*$ ]] || fail "Cannot parse Xcode version: $version"
(( BASH_REMATCH[1] >= 27 )) || fail "Xcode 27 or newer is required (selected: $version)."

if ! simctl_path=$(xcrun --find simctl); then
    fail "The selected Xcode installation does not provide simctl."
fi
if ! bridge_path=$(xcrun --find mcpbridge); then
    fail "The selected Xcode installation does not provide mcpbridge."
fi

printf 'Xcode %s\nsimctl: %s\nmcpbridge: %s\n\n' "$version" "$simctl_path" "$bridge_path"

if ! devices=$(xcrun simctl list devices available); then
    fail "Cannot query CoreSimulator. Run this check in a local terminal with access to Xcode's simulator services."
fi
printf '%s\n\n' "$devices"
if ! printf '%s\n' "$devices" | awk '
    /^-- iOS / { ios = 1; next }
    /^-- / { ios = 0 }
    ios && /\([[:xdigit:]-]+\) \((Booted|Shutdown)\)/ { found = 1 }
    END { exit !found }
'; then
    fail "No available iOS simulator found. Install an iOS runtime and create a simulator in Xcode."
fi

printf 'Xcode MCP server status:\n'
if ! xcrun mcp-server status; then
    printf 'Could not read MCP server status. Check Xcode MCP access manually.\n' >&2
fi

if ! devicectl_path=$(xcrun --find devicectl); then
    fail "The selected Xcode installation does not provide devicectl (needed for physical devices)."
fi
printf 'devicectl: %s\n\n' "$devicectl_path"

printf 'Physical devices (devicectl inventory):\n'
if physical=$(xcrun devicectl list devices 2>/dev/null); then
    printf '%s\n' "$physical" | awk 'NR > 2 && NF'
    if ! printf '%s\n' "$physical" | grep -q 'paired'; then
        printf 'No paired physical devices found. Pair over USB and tap Trust to use hardware.\n'
    fi
else
    printf 'Could not query CoreDevice. Physical-device runs need a paired, connected device.\n' >&2
fi

printf '\nToolchain and simulator inventory checks passed.\n'
printf 'Before running Artemis, review and grant Xcode MCP access for your agent.\n'
printf 'This check does not verify that device-interaction permission is granted.\n'
printf 'Physical devices additionally need a WebDriverAgent runner installed; see docs/ios.md.\n'
printf 'See docs/ios.md and https://developer.apple.com/documentation/xcode/giving-external-agents-access-to-xcode\n'
