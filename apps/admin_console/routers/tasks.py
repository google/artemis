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

import asyncio
from contextlib import suppress
import json
from typing import Any
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from artemis.core.diagnostics import readiness_engine
from artemis.runtime import DeviceExecutionLock, device_pool

try:
    from admin_console.core.state import state
    from admin_console.database.repositories.session_repository import session_repo
    from admin_console.schemas.task_schema import RunRequest
    from admin_console.services.ipc_service import ipc_service
    from admin_console.services.model_service import model_service
    from admin_console.services.task_preset_catalog import task_recommendation_engine
    from admin_console.services.task_queue_service import task_queue_service
except ImportError:
    from apps.admin_console.core.state import state
    from apps.admin_console.database.repositories.session_repository import session_repo
    from apps.admin_console.schemas.task_schema import RunRequest
    from apps.admin_console.services.ipc_service import ipc_service
    from apps.admin_console.services.model_service import model_service
    from apps.admin_console.services.task_preset_catalog import task_recommendation_engine
    from apps.admin_console.services.task_queue_service import task_queue_service


router = APIRouter(tags=["tasks"])


@router.get("/api/tasks/presets")
async def get_task_presets(
    category: str = "recommended",
    packages: str | None = None,
    limit: int = 24,
):
    """Retrieve smart task recommendations optionally tailored to detected device packages."""
    pkg_list = [p.strip() for p in packages.split(",") if p.strip()] if packages else None
    return task_recommendation_engine.recommend_tasks(
        installed_packages=pkg_list,
        category=category,
        limit=limit,
    )


@router.get("/api/tasks/catalog")
async def get_task_catalog():
    """Retrieve full catalog of predefined tasks and app package registry."""
    return {
        "tasks": [t.model_dump() for t in task_recommendation_engine.get_all_tasks()],
        "app_registry": task_recommendation_engine.get_app_registry(),
    }


@router.post("/api/run")
async def run_task(request: RunRequest):
    incoming_goals = []
    if request.goals:
        incoming_goals = request.goals
    elif request.goal:
        incoming_goals = [request.goal]

    if not incoming_goals:
        raise HTTPException(
            status_code=400,
            detail="Either 'goal' or 'goals' list must be provided.",
        )

    # Idempotent SDK retries must never re-run device readiness checks. A task
    # can hold the device while its admission response is lost in transit; in
    # that state, probing the same device again may fail or block even though
    # the original task was accepted successfully.
    if request.session_id and len(incoming_goals) == 1:
        requested_sid = str(request.session_id)
        existing_item = next(
            (
                item
                for item in state.queue_items
                if isinstance(item, dict)
                and str(item.get("session_id")) == requested_sid
            ),
            None,
        )
        persisted_session = session_repo.get_session_by_id(requested_sid)
        is_active = (
            str(state.active_session_id) == requested_sid
            or requested_sid in state.active_connections
        )
        if existing_item or persisted_session or is_active:
            task_payload = dict(existing_item or persisted_session or {})
            task_payload.setdefault("session_id", requested_sid)
            task_payload.setdefault("goal", incoming_goals[0])
            task_payload.setdefault("profile", request.profile or "flash")
            task_payload.setdefault("device_serial", request.device_serial)
            task_payload.setdefault("status", "running" if is_active else "queued")
            return {
                "status": task_payload["status"],
                "tasks": [task_payload],
                "enqueued_count": 0,
                "total_queued": len(state.queue_tasks),
            }

    # Reject an explicit unknown/offline target before running the more
    # expensive readiness probe. Besides producing a stable SDK response,
    # this avoids probing the currently active device for a serial that can
    # never be selected. Only a successful, non-empty enumeration may reject:
    # an indeterminate one (adb blip, startup) lets the submission queue and
    # fail downstream with a clear error instead.
    if request.device_serial:
        try:
            rejection = await device_pool.validate_explicit_serial_async(
                request.device_serial
            )
        except Exception:
            rejection = None
        if rejection:
            return {
                "status": "rejected",
                "error": rejection,
                "tasks": [],
                "enqueued_count": 0,
                "total_queued": len(state.queue_tasks),
            }

    # Re-check immediately before enqueueing so a device locked between UI
    # polling intervals cannot start through a stale Ready state. Use the
    # bounded submission probe: the full diagnostics path also scans packages,
    # emulator installations, Android version, and screen size.
    # With no explicit serial the probe itself resolves a live target (it
    # prefers the diagnostics target preference, then any unlocked ready
    # device); the verified serial is bound below.
    target_serial = request.device_serial
    device_probe = await readiness_engine.run_device_submission_probe(
        target_serial=target_serial
    )
    if device_probe and device_probe.summary in {"Device Locked", "Lock State Unknown"}:
        locked_serial = (
            device_probe.metadata.get("active_device", {}).get("serial")
            or target_serial
            or ""
        )
        detail = (
            f"Android device {locked_serial} is locked. Unlock it and enter the home screen before running a task.".replace("  ", " ").strip()
            if device_probe.summary == "Device Locked"
            else f"Android device {locked_serial} lock state could not be verified. Keep it unlocked on the home screen and try again.".replace("  ", " ").strip()
        )
        raise HTTPException(status_code=409, detail=detail)

    if device_probe and device_probe.metadata.get("active_device"):
        verified_serial = device_probe.metadata["active_device"].get("serial")
        # Only auto-selected targets may be re-bound to the probed device. An
        # explicitly requested serial is never silently replaced -- if it is
        # invalid, enqueue_tasks rejects the submission with a clear error.
        if verified_serial and not request.device_serial:
            target_serial = verified_serial

    return await task_queue_service.enqueue_tasks(
        incoming_goals,
        profile=request.profile or "flash",
        expected_output=request.expected_output,
        enable_outputter=request.enable_outputter,
        locked_app_package=request.locked_app_package,
        app_path=request.app_path,
        device_serial=target_serial,
        ingress=request.ingress or "frontend",
        session_id=request.session_id,
        conversation_id=request.conversation_id,
    )


@router.get("/api/devices")
async def list_devices():
    """List all connected Android devices with their busy / idle status."""
    devices = await device_pool.list_devices_async()
    return {"devices": [d.to_dict() for d in devices]}


@router.post("/api/stop")
async def stop_task(
    request: Request,
    all: bool = False,
    session_id: str | None = None,
    device_id: str | None = None,
):
    target_all = all
    target_sid = session_id
    target_dev = device_id

    try:
        body = await request.json()
        if isinstance(body, dict):
            if "all" in body:
                target_all = bool(body["all"]) or target_all
            if body.get("session_id"):
                target_sid = str(body["session_id"])
            if body.get("device_id"):
                target_dev = str(body["device_id"])
    except ValueError:
        # Empty or non-JSON body: fall back to the query parameters.
        pass

    stopped = task_queue_service.stop_tasks(
        clear_all=target_all,
        session_id=target_sid,
        device_id=target_dev,
    )
    if stopped:
        return {"status": "stopped", "session_id": target_sid}
    return {"status": "no_running_task"}



@router.post("/api/resume")
async def resume_task():
    resumed = task_queue_service.resume_task()
    if resumed:
        return {"status": "resumed"}
    return {"status": "not_paused"}


@router.get("/api/status")
async def get_status():
    # Watchdog check to ensure background worker is alive
    task_queue_service.ensure_worker_running()

    latest_session = session_repo.get_latest_session()
    latest_session_id = latest_session.get("session_id") if latest_session else None
    bg_tasks = session_repo.get_background_tasks(latest_session_id) if latest_session_id else []

    global_owner = DeviceExecutionLock.get_active_owner()
    is_running = state.is_running or global_owner is not None
    running_task = next(
        (t for t in state.queue_items if isinstance(t, dict) and t.get("status") == "running"), None
    )
    if not running_task and is_running:
        running_task = next(
            (t for t in state.queue_items if isinstance(t, dict) and t.get("status") == "pending"),
            None,
        )

    active_owners = DeviceExecutionLock.get_active_owners()
    active_tasks = [
        {
            "device_id": owner.device_id,
            "session_id": owner.session_id,
            "goal": owner.description,
            "pid": owner.pid,
            "ingress": owner.ingress,
            "acquired_at": owner.acquired_at,
        }
        for owner in active_owners.values()
    ]

    running_sid = (
        (global_owner.session_id if global_owner else None)
        or state.active_session_id
        or (running_task.get("session_id") if running_task else None)
        or (active_tasks[0]["session_id"] if active_tasks and active_tasks[0].get("session_id") else None)
        or (session_repo.get_running_session_id() if is_running else None)
    )
    owner_connection = (
        state.active_connections.get(str(running_sid), {}) if running_sid else {}
    )
    running_goal = (
        owner_connection.get("goal")
        or (global_owner.description if global_owner else None)
        or state.current_goal
        or (running_task.get("goal") if running_task else None)
        or (active_tasks[0]["goal"] if active_tasks else None)
    )

    active_profile = owner_connection.get("profile") or state.current_profile or (
        running_task.get("profile") if running_task and not global_owner else None
    )
    if not active_profile and (running_sid or latest_session_id):
        check_sid = running_sid or latest_session_id
        sess_row = session_repo.get_session_by_id(check_sid)
        if sess_row:
            llm_traces = session_repo.get_llm_traces_for_profile(check_sid)
            agent_names = session_repo.get_agent_trace_names(check_sid)
            active_profile = model_service.resolve_session_profile(
                sess_row, llm_traces, agent_names=agent_names
            )

    model_info = model_service.get_active_model_info(active_profile)

    # Unified Global Queue: merge web tasks and external SDK/CLI device queue tickets
    global_queued = DeviceExecutionLock.get_queued_tasks()
    seen_ids = set()
    queue_data: list[dict[str, Any]] = []

    for item in state.queue_tasks:
        sid = item.get("session_id")
        if sid:
            seen_ids.add(str(sid))
        queue_data.append(item)

    for g_item in global_queued:
        sid = str(g_item.get("session_id"))
        if sid not in seen_ids:
            seen_ids.add(sid)
            queue_data.append(g_item)

    if is_running:
        is_paused = state.is_paused
        return {
            "status": "paused" if is_paused else "running",
            "paused_error": state.paused_error if is_paused else None,
            "goal": running_goal,
            "pid": (
                global_owner.pid
                if global_owner
                else state.current_process.pid
                if state.current_process
                else None
            ),
            "session_id": running_sid,
            "background_tasks": bg_tasks,
            "queue": queue_data,
            "model_info": model_info,
            "active_tasks": active_tasks,
        }

    if latest_session_id and str(latest_session_id) in state.active_connections:
        conn_info = state.active_connections[str(latest_session_id)]
        is_paused = state.is_paused
        conn_profile = conn_info.get("profile") or active_profile
        conn_model_info = model_service.get_active_model_info(conn_profile)
        return {
            "status": "paused" if is_paused else "running",
            "paused_error": state.paused_error if is_paused else None,
            "goal": conn_info.get("goal"),
            "pid": conn_info.get("pid"),
            "session_id": latest_session_id,
            "background_tasks": bg_tasks,
            "queue": queue_data,
            "model_info": conn_model_info,
            "active_tasks": active_tasks,
            "ipc_port": state.ipc_port,
        }

    return {
        "status": "idle",
        "session_id": latest_session_id,
        "background_tasks": bg_tasks,
        "queue": queue_data,
        "active_tasks": active_tasks,
        "model_info": model_info,
        "ipc_port": state.ipc_port,
    }


@router.get("/api/stream")
@router.get("/api/stream/{session_id}")
async def stream_events(session_id: str = "active", client: str | None = None):
    async def event_generator():
        queue = asyncio.Queue()
        event_loop = asyncio.get_running_loop()

        def callback(event_type, data):
            try:
                # Global queue lifecycle events should always be delivered
                if event_type not in (
                    "session_started",
                    "session_ended",
                    "background_tasks_updated",
                ):
                    # Filter events by session_id when subscribed to a specific session
                    if session_id and session_id not in ("all", "active"):
                        evt_session_id = None
                        if isinstance(data, dict):
                            evt_session_id = data.get("session_id")

                        if evt_session_id and str(evt_session_id) != str(session_id):
                            return

                        if (
                            not evt_session_id
                            and state.active_session_id
                            and str(state.active_session_id) != str(session_id)
                        ):
                            return

                sanitized_data = ipc_service.sanitize_event_data(event_type, data)
                event_loop.call_soon_threadsafe(queue.put_nowait, (event_type, sanitized_data))
            except RuntimeError:
                pass

        state.add_subscriber(callback)
        yield f'event: info\ndata: {{"message": "Subscribed to session {session_id}"}}\n\n'

        if session_id in ("all", "active"):
            active_sid = state.active_session_id
            if not active_sid:
                running_item = next(
                    (t for t in state.queue_items if isinstance(t, dict) and t.get("status") == "running"),
                    None,
                )
                if running_item:
                    active_sid = running_item.get("session_id")
            if not active_sid:
                owner = DeviceExecutionLock.get_active_owner()
                if owner and owner.session_id:
                    active_sid = owner.session_id

            if active_sid:
                goal = state.current_goal or ""
                profile = state.current_profile or "flash"
                yield (
                    "event: session_started\n"
                    f"data: {json.dumps({'session_id': str(active_sid), 'initial_goal': goal, 'profile': profile}, default=str)}\n\n"
                )
                for progress_event in state.get_startup_progress(str(active_sid)):
                    yield (
                        "event: startup_progress\n"
                        f"data: {json.dumps(progress_event, default=str)}\n\n"
                    )
                try:
                    from apps.admin_console.database.repositories.step_repository import step_repo

                    recorded_steps = step_repo.get_session_steps(str(active_sid))
                    for step_dict in recorded_steps:
                        yield (
                            "event: step_recorded\n"
                            f"data: {json.dumps(step_dict, default=str)}\n\n"
                        )
                except Exception as exc:
                    print(f"[Stream] Could not replay active steps: {exc}")
        else:
            for progress_event in state.get_startup_progress(session_id):
                yield (
                    "event: startup_progress\n"
                    f"data: {json.dumps(progress_event, default=str)}\n\n"
                )

        shutdown_waiter = asyncio.create_task(state.shutdown_event.wait())
        queue_waiter = None
        try:
            while not state.is_shutting_down:
                queue_waiter = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {queue_waiter, shutdown_waiter},
                    timeout=5.0,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if shutdown_waiter in done:
                    queue_waiter.cancel()
                    with suppress(asyncio.CancelledError):
                        await queue_waiter
                    queue_waiter = None
                    break
                if queue_waiter in done:
                    event_type, data = queue_waiter.result()
                    queue_waiter = None
                    yield f"event: {event_type}\ndata: {json.dumps(data, default=str)}\n\n"
                else:
                    queue_waiter.cancel()
                    with suppress(asyncio.CancelledError):
                        await queue_waiter
                    queue_waiter = None
                    yield "event: keep-alive\ndata: {}\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            waiters = (queue_waiter, shutdown_waiter)
            for waiter in waiters:
                if waiter is not None and not waiter.done():
                    waiter.cancel()
            for waiter in waiters:
                if waiter is not None:
                    with suppress(asyncio.CancelledError):
                        await waiter
            state.remove_subscriber(callback)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
