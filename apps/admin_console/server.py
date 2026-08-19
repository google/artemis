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
def _get_showcase_dist() -> Path:
    """Return the Showcase UI Angular build directory."""
    base_dist = _workspace_root / "apps" / "showcase_ui" / "dist"
    candidates = [
        base_dist / "frontend" / "browser",
        base_dist / "browser",
        base_dist / "frontend",
        base_dist,
    ]
    for candidate in candidates:
        if candidate.is_dir() and (candidate / "index.html").exists():
            return candidate
    return base_dist / "frontend" / "browser"


@app.get("/", include_in_schema=False)
async def serve_showcase_root():
    """Explicitly serve the Showcase UI at the root path by default."""
    return await serve_showcase_spa("")


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

    clean_path = full_path.strip("/")

    # Admin / Debug Console routes
    if (
        clean_path in ("admin", "debug")
        or clean_path.startswith("admin/")
        or clean_path.startswith("debug/")
    ):
        admin_index = _admin_console_dir / "index.html"
        if admin_index.exists():
            return HTMLResponse(admin_index.read_text(encoding="utf-8"))

    showcase_dist = _get_showcase_dist()

    # Exact static file match (js, css, images, fonts, favicon, logo)
    if clean_path:
        target_file = showcase_dist / clean_path
        if target_file.is_file():
            return FileResponse(target_file)

        # Fallback for icons/logos if showcase UI hasn't been built yet
        public_file = _workspace_root / "apps" / "showcase_ui" / "public" / clean_path
        if public_file.is_file():
            return FileResponse(public_file)

    # Serve Showcase UI Angular SPA index.html
    index_file = showcase_dist / "index.html"
    if index_file.exists():
        return HTMLResponse(index_file.read_text(encoding="utf-8"))

    # Fallback message if Showcase UI is not built
    fallback_html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Artemis Showcase UI - Not Built</title>
    <style>
        body {
            font-family: system-ui, -apple-system, sans-serif;
            background: #0f172a;
            color: #f8fafc;
            display: flex;
            align-items: center;
            justify-content: center;
            height: 100vh;
            margin: 0;
        }
        .card {
            background: #1e293b;
            padding: 2.5rem;
            border-radius: 1rem;
            box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.5);
            max-width: 500px;
            text-align: center;
            border: 1px solid #334155;
        }
        h1 { color: #38bdf8; margin-top: 0; }
        code {
            background: #0f172a;
            padding: 0.25rem 0.5rem;
            border-radius: 0.375rem;
            color: #e2e8f0;
            font-size: 0.9em;
        }
        .btn {
            display: inline-block;
            margin-top: 1.5rem;
            padding: 0.75rem 1.5rem;
            background: #38bdf8;
            color: #0f172a;
            font-weight: bold;
            text-decoration: none;
            border-radius: 0.5rem;
            transition: background 0.2s;
        }
        .btn:hover { background: #0284c7; color: #fff; }
    </style>
</head>
<body>
    <div class="card">
        <h1>✨ Artemis Showcase UI</h1>
        <p>The Showcase UI (Angular frontend) has not been built yet.</p>
        <p>To compile the Showcase UI, run:</p>
        <p><code>cd apps/showcase_ui && npm install && npm run build</code></p>
        <p>Or launch using <code>./start.sh</code> or <code>artemis ui</code> to build automatically.</p>
        <a class="btn" href="/admin">Go to Admin Debug Console →</a>
    </div>
</body>
</html>"""
    return HTMLResponse(fallback_html)


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
