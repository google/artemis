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

# Install necessary build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    adb \
    && rm -rf /var/lib/apt/lists/*

# Copy application source code and configuration
COPY artemis/ /app/artemis/
COPY apps/ /app/apps/
COPY mcp_server/ /app/mcp_server/
COPY config/ /app/config/
COPY pyproject.toml setup.py README.md LICENSE /app/

# Install dependencies and artemis package
RUN pip install --no-cache-dir -e /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV ARTEMIS_APP_DIR=/app
ENV PORT=8080

EXPOSE 8080

# The console has no user authentication: publish the port to loopback only,
# e.g. `docker run -p 127.0.0.1:8080:8080 ...`, and reach it remotely through
# a Tailscale/SSH tunnel. (0.0.0.0 here is container-internal and required
# for the port mapping to work.)
CMD ["uvicorn", "apps.admin_console.server:app", "--host", "0.0.0.0", "--port", "8080"]
