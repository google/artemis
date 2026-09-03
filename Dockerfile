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

FROM node:22-bookworm-slim AS frontend-builder

WORKDIR /build/apps/showcase_ui
COPY apps/showcase_ui/package.json apps/showcase_ui/package-lock.json ./
RUN npm ci
COPY apps/showcase_ui/angular.json apps/showcase_ui/tsconfig*.json ./
COPY apps/showcase_ui/public/ ./public/
COPY apps/showcase_ui/src/ ./src/
RUN npm run build


FROM python:3.12-slim AS wheel-builder

WORKDIR /build
COPY artemis/ ./artemis/
COPY apps/ ./apps/
COPY mcp_server/ ./mcp_server/
COPY packages/artemis-client/ ./packages/artemis-client/
COPY config/ ./config/
COPY pyproject.toml setup.py README.md LICENSE ./
COPY --from=frontend-builder /build/apps/showcase_ui/dist/ ./apps/showcase_ui/dist/

# Build wheels for the runtime image.
RUN python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels ./packages/artemis-client \
    && python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .


FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    adb \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=wheel-builder /wheels/ /wheels/
RUN pip install --no-cache-dir /wheels/artemis_client-*.whl \
    && pip install --no-cache-dir /wheels/artemis-*.whl \
    && rm -rf /wheels

ENV PYTHONUNBUFFERED=1
ENV ARTEMIS_APP_DIR=/app
ENV PORT=8080

EXPOSE 8080

# The console has no user authentication: publish the port to loopback only,
# e.g. `docker run -p 127.0.0.1:8080:8080 ...`, and reach it remotely through
# a Tailscale/SSH tunnel. (0.0.0.0 here is container-internal and required
# for the port mapping to work.)
CMD ["uvicorn", "apps.admin_console.server:app", "--host", "0.0.0.0", "--port", "8080"]
