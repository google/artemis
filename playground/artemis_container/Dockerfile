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

FROM python:3.12-slim

WORKDIR /app

# Install system dependencies, Android toolchain (adb), OpenCV runtime, FFmpeg, and curl
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    adb \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy core Artemis package structure and configuration from repository root
COPY artemis/ /app/artemis/
COPY apps/ /app/apps/
COPY mcp_server/ /app/mcp_server/
COPY config/ /app/config/
COPY pyproject.toml setup.py README.md LICENSE /app/

# Install dependencies and artemis in editable mode
RUN pip install --no-cache-dir -e /app

# Copy custom container entrypoint script
COPY playground/artemis_container/docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

# Environment Configuration
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV ARTEMIS_APP_DIR=/app
ENV PORT=8080

# Expose internal Artemis Admin Console and Stream port
EXPOSE 8080

# Health check to ensure Artemis Admin Console is alive
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:8080/api/system/health || curl -f http://localhost:8080/ || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "apps.admin_console.server:app", "--host", "0.0.0.0", "--port", "8080"]
