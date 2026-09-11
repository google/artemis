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

# Usage: ./playground/deploy_to_cos.sh <COS_VM_NAME> [ZONE] [PROJECT]
if [ "$#" -lt 1 ]; then
    echo "Usage: $0 <COS_VM_NAME> [ZONE] [PROJECT]"
    echo ""
    echo "Example:"
    echo "  $0 my-cos-instance us-central1-a"
    echo "  $0 my-cos-instance us-central1-a my-gcp-project"
    exit 1
fi

VM_NAME="$1"
ZONE="${2:-}"
PROJECT="${3:-}"

SSH_OPTS=()
if [ -n "$ZONE" ]; then
    SSH_OPTS+=(--zone="$ZONE")
fi
if [ -n "$PROJECT" ]; then
    SSH_OPTS+=(--project="$PROJECT")
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "================================================================="
echo "  Deploying Artemis to Container-Optimized OS (COS) VM"
echo "  Target VM : ${VM_NAME}"
echo "  Zone      : ${ZONE:-'(default)'}"
echo "  Project   : ${PROJECT:-'(default)'}"
echo "================================================================="

# ----------------------------------------------------------------------
# Step 1: Build Docker Images on local gLinux workstation (linux/amd64)
# ----------------------------------------------------------------------
echo ""
echo "[Step 1/3] Building Docker images locally on gLinux (linux/amd64)..."

echo "  -> Building artemis:latest..."
docker build --platform linux/amd64 -f playground/artemis_container/Dockerfile -t artemis:latest .

echo "  -> Building artemis-backend-manager:latest..."
docker build --platform linux/amd64 -t artemis-backend-manager:latest playground/backend_manager

echo "  -> Building artemis-nginx-proxy:latest..."
docker build --platform linux/amd64 -t artemis-nginx-proxy:latest playground/nginx_container

echo "[Step 1/3] All images successfully built locally."

# ----------------------------------------------------------------------
# Step 2: Stream & Load Docker Images into COS VM via SSH Pipe
# ----------------------------------------------------------------------
echo ""
echo "[Step 2/3] Transferring and loading images into COS VM via compressed SSH stream..."

stream_image() {
    local img="$1"
    echo "  -> Transferring ${img}..."
    docker save "$img" | gzip -1 | gcloud compute ssh "$VM_NAME" "${SSH_OPTS[@]}" -- "gunzip | docker load"
}

stream_image "artemis:latest"
stream_image "artemis-backend-manager:latest"
stream_image "artemis-nginx-proxy:latest"

echo "[Step 2/3] All images loaded into COS VM Docker daemon."

# ----------------------------------------------------------------------
# Step 3: Launch / Restart Artemis Services on the COS VM
# ----------------------------------------------------------------------
echo ""
echo "[Step 3/3] Starting containers on COS VM..."

gcloud compute ssh "$VM_NAME" "${SSH_OPTS[@]}" -- bash -s << 'EOF'
set -euo pipefail

echo "  [COS] Ensuring shared Docker network (artemis-net)..."
docker network create artemis-net 2>/dev/null || true

echo "  [COS] Stopping previous container instances if running..."
docker rm -f backend artemis-nginx-proxy 2>/dev/null || true

echo "  [COS] Starting Backend Manager container..."
docker run -d \
  --name backend \
  --restart unless-stopped \
  --network artemis-net \
  --add-host host.docker.internal:host-gateway \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e APP_ENV=production \
  -e PORT=8000 \
  -e DOCKER_NETWORK=artemis-net \
  -e ARTEMIS_IMAGE=artemis:latest \
  -e CLOUD_ORCHESTRATOR_URL=http://cloud-orchestrator:2081 \
  -e CUTTLEFISH_HOST_GATEWAY=cloud-orchestrator \
  -e USE_LOCAL_FALLBACK_DB=true \
  artemis-backend-manager:latest

echo "  [COS] Starting Nginx Reverse Proxy container..."
docker run -d \
  --name artemis-nginx-proxy \
  --restart unless-stopped \
  --network artemis-net \
  --add-host host.docker.internal:host-gateway \
  -p 80:80 \
  -p 443:443 \
  artemis-nginx-proxy:latest

echo "  [COS] Verifying active containers..."
docker ps --filter "network=artemis-net"
EOF

echo ""
echo "================================================================="
echo "  Deployment Complete! 🚀"
echo "  Artemis Reverse Proxy is listening on ports 80 & 443 of ${VM_NAME}."
echo "================================================================="
