#!/bin/bash
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

set -e

echo "=================================================="
echo "  ARTEMIS SESSION CONTAINER INITIALIZATION"
echo "  Session ID : ${SESSION_ID:-standalone-dev}"
echo "  ADB Target : ${ADB_DEVICE_SERIAL:-not-configured}"
echo "=================================================="

# Start ADB daemon inside container
adb start-server

# If ADB target is specified, attempt connection with retry loop
if [ -n "$ADB_DEVICE_SERIAL" ]; then
    # Resolve host gateway IP if targeting host.docker.internal on Linux bridge
    TARGET_SERIAL="$ADB_DEVICE_SERIAL"
    if [[ "$ADB_DEVICE_SERIAL" == host.docker.internal:* ]]; then
        PORT_PART="${ADB_DEVICE_SERIAL#host.docker.internal:}"
        DEFAULT_GW=$(ip route show default 2>/dev/null | awk '{print $3}' | head -n1)
        if [ -n "$DEFAULT_GW" ]; then
            echo "[Artemis Entrypoint] Detected bridge gateway IP: ${DEFAULT_GW} for host.docker.internal"
            TARGET_SERIAL="${DEFAULT_GW}:${PORT_PART}"
        fi
    fi

    echo "[Artemis Entrypoint] Connecting to target Android device at ${TARGET_SERIAL}..."
    
    MAX_RETRIES=15
    RETRY_COUNT=0
    CONNECTED=false

    while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
        echo "[Artemis Entrypoint] Attempting adb connect ${TARGET_SERIAL} (Attempt $((RETRY_COUNT + 1))/${MAX_RETRIES})..."
        CONNECT_OUTPUT=$(adb connect "${TARGET_SERIAL}" 2>&1 || true)
        echo "  -> ${CONNECT_OUTPUT}"

        # Check if connected
        if echo "${CONNECT_OUTPUT}" | grep -q "connected to"; then
            CONNECTED=true
            break
        fi

        RETRY_COUNT=$((RETRY_COUNT + 1))
        sleep 2
    done

    if [ "$CONNECTED" = true ]; then
        echo "[Artemis Entrypoint] ADB successfully connected to ${TARGET_SERIAL}."
    else
        echo "[Artemis Entrypoint] Warning: Could not establish immediate ADB connection to ${TARGET_SERIAL}. Container will continue; agent will reconnect on demand."
    fi
fi

# List currently attached devices
echo "[Artemis Entrypoint] Active ADB Devices:"
adb devices -l

echo "[Artemis Entrypoint] Starting Artemis Admin Console & Agent Server on port ${PORT:-8080}..."
exec "$@"
