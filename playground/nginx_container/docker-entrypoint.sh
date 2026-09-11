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

CERT_DIR="/etc/nginx/certs"
CERT_FILE="${CERT_DIR}/tls.crt"
KEY_FILE="${CERT_DIR}/tls.key"

echo "[Nginx Entrypoint] Initializing Artemis Ingress Proxy..."

# Check if SSL certificates exist; if not, auto-generate a self-signed fallback cert
if [ ! -f "$CERT_FILE" ] || [ ! -f "$KEY_FILE" ]; then
    echo "[Nginx Entrypoint] No SSL certificate found at ${CERT_FILE}. Generating self-signed certificate for development/staging..."
    mkdir -p "${CERT_DIR}"
    openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
        -keyout "${KEY_FILE}" \
        -out "${CERT_FILE}" \
        -subj "/CN=localhost/O=Artemis/C=US" \
        -addext "subjectAltName=DNS:localhost,DNS:*.localhost,IP:127.0.0.1" \
        2>/dev/null
    chmod 600 "${KEY_FILE}"
    chmod 644 "${CERT_FILE}"
    echo "[Nginx Entrypoint] Self-signed certificate generated successfully."
else
    echo "[Nginx Entrypoint] SSL certificate verified at ${CERT_FILE}."
fi

# Test Nginx configuration validity before starting
echo "[Nginx Entrypoint] Verifying Nginx configuration syntax..."
nginx -t

echo "[Nginx Entrypoint] Starting Nginx daemon..."
exec "$@"
