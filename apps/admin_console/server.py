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

"""Artemis Admin & Trace Console Server

Modular entrypoint for full trace inspection, step replay, and task execution management.
"""

import asyncio
import os
from pathlib import Path
import sys

# Bootstrap sys.path to allow running from any CWD
_current_p = Path(__file__).resolve().parent
while _current_p != _current_p.parent:
    if (_current_p / "pyproject.toml").exists() or (_current_p / "artemis").is_dir():
        _workspace_root = _current_p
        break
    _current_p = _current_p.parent
else:
    _workspace_root = Path(__file__).resolve().parent.parent.parent

_apps_dir = _workspace_root / "apps"
_admin_console_dir = _apps_dir / "admin_console"
_cloud_service_dir = _apps_dir / "cloud_service"

for _p in (str(_workspace_root), str(_apps_dir), str(_admin_console_dir), str(_cloud_service_dir)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

try:
    from admin_console.core.config import (
        DB_PATH,
        IMAGES_DIR,
        REPLAY_BASE_DIR,
        TEST_DATA_DIR,
        TEST_OUTPUTS_DIR,
        TRACES_PATH,
        WORKSPACE_ROOT,
        init_ls_address,
    )
    from admin_console.core.state import ServerState, state
    from admin_console.database.connection import db_session, get_db
    from admin_console.database.repositories.session_repository import session_repo
    from admin_console.database.repositories.step_repository import step_repo
    from admin_console.database.repositories.trace_repository import trace_repo
    from admin_console.routers import media, replay, sessions, steps, stream, system, tasks
    from admin_console.routers.replay import replay_manager
    from admin_console.services.ipc_service import ipc_service
    from admin_console.services.media_service import media_service
    from admin_console.services.model_service import model_service
    from admin_console.services.task_queue_service import task_queue_service
except ImportError:
    from apps.admin_console.core.config import (
        init_ls_address,
    )
    from apps.admin_console.core.state import state
    from apps.admin_console.database.repositories.session_repository import session_repo
    from apps.admin_console.routers import media, replay, sessions, steps, stream, system, tasks
    from apps.admin_console.routers.replay import replay_manager
    from apps.admin_console.services.ipc_service import ipc_service
    from apps.admin_console.services.model_service import model_service
    from apps.admin_console.services.task_queue_service import task_queue_service

# Initialize language server synchronization address
init_ls_address()

# Initialize FastAPI application
app = FastAPI(title="Artemis Admin & Trace Console")

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    """Startup lifecycle hooks."""
    asyncio.create_task(asyncio.to_thread(task_queue_service.archive_older_replays_on_launch))
    asyncio.create_task(
        asyncio.to_thread(task_queue_service.verify_chunks_exist_on_launch, replay_manager)
    )

    cleaned = session_repo.cleanup_orphans_on_startup()
    if cleaned > 0:
        print(f"[ServerStartup] Marked {cleaned} orphaned running session(s) as failed.")

    await ipc_service.start_server()
    state.worker_task = asyncio.create_task(task_queue_service.queue_worker())


# Mount modular routers
app.include_router(stream.router)
app.include_router(media.router)
app.include_router(sessions.router)
app.include_router(steps.router)
app.include_router(tasks.router)
app.include_router(replay.router)
app.include_router(system.router)

# Mount cloud gateway router for Frappe / Cloud integration if present
try:
    from cloud_service.gateway import cloud_router

    app.include_router(cloud_router)
except ImportError:
    try:
        from apps.cloud_service.gateway import cloud_router

        app.include_router(cloud_router)
    except ImportError:
        pass


# ------------------------------------------------------------------------------
# Showcase UI (Angular 19) Unified Single-Port SPA Static Hosting
# ------------------------------------------------------------------------------
_showcase_dist = _workspace_root / "apps" / "showcase_ui" / "dist" / "frontend" / "browser"
if not _showcase_dist.exists():
    _showcase_dist = _workspace_root / "apps" / "showcase_ui" / "dist"

if _showcase_dist.is_dir() and (_showcase_dist / "index.html").exists():

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_showcase_spa(full_path: str):
        # Do not intercept API, media, or replay paths
        if (
            full_path.startswith("api/")
            or full_path.startswith("images/")
            or full_path.startswith("videos/")
            or full_path.startswith("local_file")
            or full_path == "docs"
            or full_path == "openapi.json"
            or full_path.startswith("redoc")
        ):
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="Endpoint not found")

        # Admin / Debug Console routes
        if full_path in ("admin", "debug"):
            admin_index = _admin_console_dir / "index.html"
            if admin_index.exists():
                return HTMLResponse(admin_index.read_text(encoding="utf-8"))

        # Exact static file match (js, css, images, fonts, favicon, logo)
        target_file = _showcase_dist / full_path
        if full_path and target_file.is_file():
            return FileResponse(target_file)

        # Default fallback to Angular SPA index.html
        index_file = _showcase_dist / "index.html"
        return HTMLResponse(index_file.read_text(encoding="utf-8"))
else:

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def fallback_root():
        admin_index = _admin_console_dir / "index.html"
        if admin_index.exists():
            return HTMLResponse(admin_index.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>Artemis Console</h1>")


# ------------------------------------------------------------------------------
# Backward compatibility exports
# ------------------------------------------------------------------------------
get_active_model_info = model_service.get_active_model_info
start_ipc_server = ipc_service.start_server
queue_worker = task_queue_service.queue_worker
active_connections = state.active_connections
ipc_subscribers = state.ipc_subscribers
task_queue = state.task_queue
queue_goals = state.queue_goals


def main():
    import uvicorn

    port = int(os.environ.get("ANTIGRAVITY_SIDECAR_WEB_PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
