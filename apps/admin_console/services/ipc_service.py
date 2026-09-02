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
import logging
from typing import Any

logger = logging.getLogger(__name__)

try:
    from admin_console.core.state import state
except ImportError:
    from apps.admin_console.core.state import state


class IPCService:
    """Service managing internal IPC socket server and event distribution."""

    @staticmethod
    def _clean_value(val: Any) -> Any:
        """Recursively cleans values to remove non-serializable and internal
        Python runtime representations.
        """
        if val is None:
            return None
        if isinstance(val, (bool, int, float)):
            return val
        if isinstance(val, str):
            if (
                "object at 0x" in val
                or val.startswith("<artemis.")
                or val.startswith("<controller")
            ):
                return None
            return val
        if isinstance(val, dict):
            cleaned = {}
            for k, v in val.items():
                cv = IPCService._clean_value(v)
                if cv is not None:
                    cleaned[k] = cv
            return cleaned
        if isinstance(val, (list, tuple)):
            cleaned_list = []
            for item in val:
                cv = IPCService._clean_value(item)
                if cv is not None:
                    cleaned_list.append(cv)
            return cleaned_list
        val_str = str(val)
        if "object at 0x" in val_str or val_str.startswith("<"):
            return None
        return val_str

    @classmethod
    def sanitize_event_data(cls, event_type: str, data: Any) -> Any:
        """Sanitizes stream event payloads for all clients, stripping bulky
        raw bytes and internal memory references.
        """
        if not isinstance(data, dict):
            return data

        if event_type == "trace_recorded":
            tr_type = data.get("type")
            payload = data.get("payload")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except ValueError:
                    # Non-JSON payload string: cleaned as-is below.
                    pass

            if tr_type in ("tool", "action") and isinstance(payload, dict):
                args = payload.get("args", payload)
                data["payload"] = {"args": cls._clean_value(args)}
            elif tr_type == "llm_call" and isinstance(payload, dict):
                data["payload"] = {"error": cls._clean_value(payload.get("error"))}
            elif payload is not None:
                data["payload"] = cls._clean_value(payload)

        elif event_type in ("step_recorded", "step_updated"):
            # Strip heavy raw image bytes from SSE broadcast (clients fetch via image URLs)
            data["pre_screenshot_bytes"] = None
            data["post_screenshot_bytes"] = None

            if "generic_tools" in data and isinstance(data["generic_tools"], list):
                sanitized_tools = []
                for tr in data["generic_tools"]:
                    if not isinstance(tr, dict):
                        continue
                    tr_dict = dict(tr)
                    pl = tr_dict.get("payload")
                    if isinstance(pl, str):
                        try:
                            pl = json.loads(pl)
                        except ValueError:
                            # Non-JSON payload string: cleaned as-is below.
                            pass

                    if tr_dict.get("type") == "llm_call":
                        tr_dict["payload"] = (
                            {"error": cls._clean_value(pl.get("error"))}
                            if isinstance(pl, dict)
                            else None
                        )
                    elif isinstance(pl, dict):
                        args = pl.get("args", pl)
                        tr_dict["payload"] = {"args": cls._clean_value(args)}
                    else:
                        tr_dict["payload"] = cls._clean_value(pl)
                    sanitized_tools.append(tr_dict)
                data["generic_tools"] = sanitized_tools

            try:
                from artemis.utils.coordinates import normalize_step_actions

                data = normalize_step_actions(data)
            except ImportError:
                pass

        return data

    # Backward compatibility alias
    filter_event_for_angular = sanitize_event_data

    @classmethod
    async def start_server(cls):
        async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            current_session_id = None
            try:
                while not state.is_shutting_down:
                    line = await reader.readline()
                    if not line:
                        break
                    try:
                        payload = json.loads(line.decode("utf-8"))
                        event_type = payload.get("event_type")
                        data = payload.get("data")

                        if event_type == "session_started" and data:
                            incoming_sid = data.get("session_id")
                            if incoming_sid and str(incoming_sid) in getattr(
                                state, "cancelled_session_ids", set()
                            ):
                                print(
                                    f"[IPC] Ignoring session_started for cancelled session: {incoming_sid}"
                                )
                                continue

                            state.active_session_id = incoming_sid
                            current_session_id = incoming_sid
                            pid = data.get("pid")
                            goal = data.get("initial_goal")
                            device_info = data.get("device_info")
                            if isinstance(device_info, dict) and device_info.get("profile"):
                                state.current_profile = device_info.get("profile")
                            if goal:
                                state.current_goal = goal
                            print(
                                f"[IPC] Session started: {state.active_session_id} (profile: {state.current_profile})"
                            )
                            if current_session_id:
                                state.active_connections[str(current_session_id)] = {
                                    "writer": writer,
                                    "pid": pid,
                                    "goal": goal,
                                    "profile": state.current_profile,
                                }
                        elif event_type == "session_ended" and data:
                            ended_sid = data.get("session_id")
                            if state.active_session_id and str(state.active_session_id) == str(
                                ended_sid
                            ):
                                state.active_session_id = None
                                state.current_goal = None
                                state.current_profile = None

                        if event_type == "startup_progress" and isinstance(data, dict):
                            state.record_startup_progress(data)

                        for cb in list(state.ipc_subscribers):
                            try:
                                cb(event_type, data)
                            except Exception:
                                # One broken subscriber must not block the
                                # others, but a silent drop hides it entirely.
                                logger.warning(
                                    "IPC subscriber %r failed for event %s",
                                    cb,
                                    event_type,
                                    exc_info=True,
                                )
                    except (ValueError, UnicodeDecodeError, TypeError) as e:
                        print(f"IPC parse error: {e}")
            except asyncio.CancelledError:
                raise
            except (ConnectionResetError, BrokenPipeError, OSError):
                # Normal when a task or the UI server is shutting down on Windows.
                pass
            finally:
                if current_session_id and str(current_session_id) in state.active_connections:
                    state.active_connections.pop(str(current_session_id), None)
                writer.close()
                with suppress(ConnectionResetError, BrokenPipeError, OSError):
                    await writer.wait_closed()

        server = await asyncio.start_server(handle_client, "127.0.0.1", 0, limit=1024 * 1024 * 100)
        state.ipc_port = server.sockets[0].getsockname()[1]
        state.ipc_server = server
        state.ipc_serve_task = asyncio.create_task(server.serve_forever())
        print(f"Internal IPC server started on port {state.ipc_port}")
        try:
            from artemis.config import write_ipc_port

            write_ipc_port(state.ipc_port)
        except Exception as e:
            print(f"Failed to write IPC port file: {e}")

    @classmethod
    async def stop_server(cls):
        """Close IPC listeners and task connections during application shutdown."""
        for connection in list(state.active_connections.values()):
            writer = connection.get("writer")
            if writer is not None:
                writer.close()
                with suppress(ConnectionResetError, BrokenPipeError, OSError):
                    await writer.wait_closed()
        state.active_connections.clear()

        if state.ipc_server is not None:
            state.ipc_server.close()
            await state.ipc_server.wait_closed()
            state.ipc_server = None

        if state.ipc_serve_task is not None:
            state.ipc_serve_task.cancel()
            with suppress(asyncio.CancelledError):
                await state.ipc_serve_task
            state.ipc_serve_task = None
        state.ipc_port = None
        try:
            from artemis.config import clear_ipc_port

            clear_ipc_port()
        except Exception as e:
            print(f"Failed to clear IPC port state: {e}")


ipc_service = IPCService()
