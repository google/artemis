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
from collections.abc import Callable
import hashlib
import json
import os
from pathlib import Path
import socket
import threading
import time
from typing import Any
from uuid import UUID, uuid4

from artemis.config import read_ipc_port, settings
from artemis.context import ArtemisContext
from artemis.data_engine.models import (
    BackgroundTaskRecord,
    FailedOutputRecord,
    ImageRecord,
    SessionMetadata,
    StepRecord,
    TraceRecord,
    VideoRecordingRecord,
)
from artemis.data_engine.storage import StorageManager
from artemis.data_engine.trace import CURRENT_TRACE_ID
from artemis.utils.coordinates import (
    normalize_any_structure,
    normalize_step_actions,
)
from artemis.utils.logger import get_logger
from artemis.utils.text import safe_extract_text

logger = get_logger(__name__)

_CURRENT_DATA_ENGINE = None


class DataEngine:
    """Core engine for managing runtime data, storage, and streaming."""

    def __init__(self, ctx: ArtemisContext):
        self.ctx = ctx

        # Determine paths from context
        if ctx.execution_setup and ctx.execution_setup.traces_path:
            self.global_base_dir = Path(ctx.execution_setup.traces_path)
            self.db_path = self.global_base_dir / "data_engine.db"
        else:
            # Fallback to default unified settings
            self.global_base_dir = settings.TRACES_PATH
            self.db_path = settings.DATA_ENGINE_DB_PATH

        self.storage = StorageManager(self.db_path, self.global_base_dir)

        self.current_session_id: UUID | None = None
        self.session_start_time: float | None = None
        self.current_step_id: UUID | None = None
        self.last_recorded_step_id: UUID | None = None
        self.current_step_dir: Path | None = None
        self.subscribers: list[Callable[[str, Any], None]] = []
        self.lock = threading.Lock()
        self._trace_counter = 0
        self._pending_tasks = set()
        self._pending_threads = []
        self._accumulated_logs = {}
        self._bg_task_to_trace_id = {}
        # Background tasks are written directly to SQLite storage

        self.ipc_socket = None

        ipc_port = read_ipc_port()
        if ipc_port:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.connect(("127.0.0.1", ipc_port))
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.ipc_socket = s
                logger.info(f"Connected to IPC server on port {ipc_port} with TCP_NODELAY")
            except Exception as e:
                logger.error(f"Failed to connect to IPC server on port {ipc_port}: {e}")

    @property
    def base_dir(self) -> Path:
        if self.current_session_id:
            return self.global_base_dir / str(self.current_session_id)
        return self.global_base_dir

    def subscribe(self, callback: Callable[[str, Any], None]):
        """Subscribe to real-time events (e.g., for SSE)."""
        self.subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[str, Any], None]):
        """Unsubscribe from real-time events."""
        if callback in self.subscribers:
            self.subscribers.remove(callback)

    def _publish(self, event_type: str, data: Any):
        """Publish event to all subscribers."""
        if isinstance(data, dict) and "session_id" not in data and self.current_session_id:
            data["session_id"] = str(self.current_session_id)

        for callback in self.subscribers:
            try:
                callback(event_type, data)
            except Exception as e:
                logger.error(f"Error in subscriber callback: {e}")

        # Bridge to Cloud Gateway SSE stream in Cloud Mode
        if os.environ.get("ARTEMIS_CLOUD_MODE") == "1":
            try:
                import asyncio
                from debug_pub.cloud_gateway import cloud_manager

                sid = os.environ.get("ARTEMIS_CLOUD_SESSION_ID") or (
                    str(self.current_session_id) if self.current_session_id else None
                )
                if sid and sid in cloud_manager.sessions:
                    try:
                        loop = asyncio.get_running_loop()
                        if loop.is_running():
                            loop.create_task(
                                cloud_manager.sessions[sid].emit_event(
                                    event_type,
                                    data if isinstance(data, dict) else {"payload": data},
                                )
                            )
                    except RuntimeError:
                        pass
            except Exception:
                pass

        # Auto-reconnect to IPC socket if disconnected or not yet connected
        if not getattr(self, "ipc_socket", None):
            ipc_port = read_ipc_port()
            if ipc_port:
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.connect(("127.0.0.1", ipc_port))
                    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    self.ipc_socket = s
                except Exception:
                    self.ipc_socket = None

        if getattr(self, "ipc_socket", None):
            try:
                payload = json.dumps({"event_type": event_type, "data": data}, default=str) + "\n"
                self.ipc_socket.sendall(payload.encode("utf-8"))
            except Exception:
                try:
                    self.ipc_socket.close()
                except Exception:
                    pass
                self.ipc_socket = None

    def start_session(self, goal: str, device_info: dict[str, Any] | None = None) -> UUID:
        """Start a new session."""
        global _CURRENT_DATA_ENGINE
        _CURRENT_DATA_ENGINE = self

        env_session_id = os.getenv("ARTEMIS_CLOUD_SESSION_ID") or os.getenv("ARTEMIS_SESSION_ID")
        if env_session_id:
            try:
                session_id = UUID(env_session_id)
            except ValueError:
                session_id = env_session_id
        else:
            session_id = uuid4()
        self.current_session_id = session_id
        self.session_start_time = time.time()

        # Clear old pause file if it exists
        pause_file = self.global_base_dir.parent / ".artemis_paused"
        if pause_file.exists():
            try:
                pause_file.unlink()
                logger.info("Removed old pause file on session start.")
            except Exception as e:
                logger.error(f"Failed to delete old pause file: {e}")

        session = SessionMetadata(
            session_id=session_id,
            initial_goal=goal,
            start_time=self.session_start_time,
            device_info=device_info or {},
            pid=os.getpid(),
        )
        self.storage.create_session(session)
        self.current_step_number = 0
        logger.info(f"Session started: {session_id}")
        self._publish("session_started", session.model_dump())
        return session_id

    def end_session(self, status: str = "completed"):
        """End the current session, updating its status and end time."""
        if not self.current_session_id:
            return

        # Clear pause file if it exists
        if hasattr(self, "global_base_dir") and self.global_base_dir:
            pause_file = self.global_base_dir.parent / ".artemis_paused"
            if pause_file.exists():
                try:
                    pause_file.unlink()
                    logger.info("Removed pause file on session end.")
                except Exception as e:
                    logger.error(f"Failed to delete pause file on session end: {e}")

        session_id = self.current_session_id
        end_time = time.time()
        session = self.storage.get_session(session_id)
        if session:
            session.end_time = end_time
            session.status = status
        else:
            session = SessionMetadata(
                session_id=session_id,
                initial_goal="",
                start_time=self.session_start_time or end_time,
                end_time=end_time,
                status=status,
                device_info=self.ctx.device.model_dump()
                if getattr(self, "ctx", None) and self.ctx.device
                else {},
            )

        try:
            self.storage.update_session(session)
            logger.info(f"Session ended: {session_id} with status: {status}")
            self._publish("session_ended", session.model_dump())
        except Exception as e:
            logger.error(f"Failed to end session in DataEngine: {e}")

    def record_video_start(
        self,
        video_id: UUID,
        device_id: str,
        local_video_path: str | Path,
        start_time: float | None = None,
    ):
        """Record the start of a video recording."""
        if not self.storage:
            return
        try:
            record = VideoRecordingRecord(
                video_id=video_id,
                session_id=self.current_session_id,
                device_id=device_id,
                start_time=start_time or time.time(),
                local_video_path=str(local_video_path),
            )
            self.storage.create_video_recording(record)
        except Exception as e:
            logger.error(f"Failed to record video start in DataEngine: {e}")

    def record_video_stop(
        self,
        video_id: UUID,
        device_id: str,
        local_video_path: str | Path,
        start_time: float,
        end_time: float | None = None,
    ):
        """Record the completion of a video recording and sync to session metadata."""
        if not self.storage:
            return
        try:
            record = VideoRecordingRecord(
                video_id=video_id,
                session_id=self.current_session_id,
                device_id=device_id,
                start_time=start_time,
                end_time=end_time or time.time(),
                local_video_path=str(local_video_path),
            )
            self.storage.update_video_recording(record)
        except Exception as e:
            logger.error(f"Failed to record video stop in DataEngine: {e}")

    def update_video_path(self, local_video_path: str | Path):
        """Update the video path across tables when traces or videos are moved."""
        if not self.storage or not self.current_session_id:
            return
        try:
            self.storage.update_session_video_path(self.current_session_id, str(local_video_path))
        except Exception as e:
            logger.warning(f"Failed to update video path in DataEngine: {e}")

    def get_or_create_image(
        self,
        image_bytes: bytes,
        ui_tree: Any | None = None,
        ocr_result: Any | None = None,
    ) -> str:
        """Get image name by hash, or create new record if not exists."""

        hasher = hashlib.sha256()
        hasher.update(image_bytes)
        image_name = hasher.hexdigest()

        image_record = self.storage.get_image(image_name)
        if image_record:
            # If the image exists but is missing OCR or UI tree, and we now have them, update the record.
            if (ocr_result is not None and image_record.ocr_result is None) or (
                ui_tree is not None and image_record.ui_tree is None
            ):
                self.storage.update_image_data(image_name, ocr_result, ui_tree)
            return image_name

        images_dir = self.global_base_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        file_path = images_dir / f"{image_name}.jpg"

        with open(file_path, "wb") as f:
            f.write(image_bytes)

        new_record = ImageRecord(
            image_name=image_name,
            ui_tree=ui_tree,
            ocr_result=ocr_result,
            extra_metadata={},
        )
        self.storage.create_image(new_record)

        return image_name

    def get_image_path(self, image_name: str) -> Path:
        """Get the absolute file path of an image by its name/hash."""
        return self.global_base_dir / "images" / f"{image_name}.jpg"

    def record_step(
        self,
        pre_screenshot_bytes: bytes | None = None,
        post_screenshot_bytes: bytes | None = None,
        ui_tree: Any | None = None,
        ocr_result: Any | None = None,
        foreground_app: str | None = None,
        action_taken: dict[str, Any] | None = None,
        summary: str | None = None,
        operator_raw_thinking: str | None = None,
        operator_native_thinking: str | None = None,
        last_execution_result: Any | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> UUID:
        """Record a step, saving screenshots and metadata."""
        if not self.current_session_id:
            raise ValueError("No active session. Call start_session first.")

        with self.lock:
            self.current_step_number += 1
            step_number = self.current_step_number

            step_id = self.current_step_id or uuid4()
            self.last_recorded_step_id = step_id
            # Reset current_step_id so subsequent steps will allocate a new one
            self.current_step_id = None

        # Process images and step creation in background thread

        pre_image_name = (
            hashlib.sha256(pre_screenshot_bytes).hexdigest() if pre_screenshot_bytes else None
        )
        post_image_name = (
            hashlib.sha256(post_screenshot_bytes).hexdigest() if post_screenshot_bytes else None
        )
        if pre_image_name and post_image_name and pre_image_name == post_image_name:
            post_image_name = None

        step = StepRecord(
            step_id=step_id,
            session_id=self.current_session_id,
            step_number=step_number,
            timestamp=time.time(),
            pre_image_name=pre_image_name,
            post_image_name=post_image_name,
            summary=summary,
            action_taken=action_taken,
            operator_raw_thinking=operator_raw_thinking,
            operator_native_thinking=operator_native_thinking,
            last_execution_result=last_execution_result,
            extra_metadata=extra_metadata or {},
        )

        def _run_background_storage():
            if pre_screenshot_bytes:
                self.get_or_create_image(pre_screenshot_bytes, ui_tree, ocr_result)
            if post_screenshot_bytes:
                self.get_or_create_image(post_screenshot_bytes)
            self.storage.create_step(step)

        # Run storage in background
        try:
            task = asyncio.create_task(asyncio.to_thread(_run_background_storage))
            self._pending_tasks.add(task)
            task.add_done_callback(self._on_background_task_done)
        except RuntimeError:
            # Fallback if no event loop
            def run_and_cleanup():
                _run_background_storage()
                curr_thread = threading.current_thread()
                if curr_thread in self._pending_threads:
                    self._pending_threads.remove(curr_thread)

            thread = threading.Thread(target=run_and_cleanup)
            self._pending_threads.append(thread)
            thread.start()

        logger.info(f"Recorded step {step_number} for session {self.current_session_id}")

        # Publish event with relative time
        step_dict = step.model_dump()
        step_dict["relative_time"] = self.get_relative_time(step.timestamp)

        # Include generic tools in the real-time event
        try:
            traces = self.storage.get_step_traces(step_id)
            generic_tools = []
            for t in traces:
                if t.type == "tool":
                    generic_tools.append(t.model_dump())
            step_dict["generic_tools"] = generic_tools
        except Exception as e:
            logger.error(f"Failed to fetch step traces for SSE: {e}")
            step_dict["generic_tools"] = []

        # Extract and attach token usage for real-time SSE stream
        step_p, step_c, step_t = 0, 0, 0
        if extra_metadata and isinstance(extra_metadata, dict) and "token_usage" in extra_metadata:
            u = extra_metadata["token_usage"]
            if isinstance(u, dict):
                step_p = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
                step_c = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
                step_t = int(u.get("total_tokens") or (step_p + step_c))

        if step_t == 0:
            token_info = self._get_step_token_usage_for_sse(step_id)
            if token_info:
                step_p = token_info["prompt_tokens"]
                step_c = token_info["completion_tokens"]
                step_t = token_info["total_tokens"]

        if step_t > 0:
            step_dict["token_usage"] = {
                "prompt_tokens": step_p,
                "completion_tokens": step_c,
                "total_tokens": step_t,
            }
            step_dict["total_tokens"] = step_t

        step_dict = normalize_step_actions(step_dict)

        self._publish("step_recorded", step_dict)

        return step_id

    def record_trace(
        self,
        type: str,
        name: str,
        payload: dict[str, Any],
        step_id: UUID | None = None,
        parent_trace_id: UUID | None = None,
        status: str = "success",
        duration: float | None = None,
        trace_id: UUID | None = None,
    ) -> UUID:
        """Record a trace (agent, tool, or log), non-blocking."""
        if not self.current_session_id:
            raise ValueError("No active session. Call start_session first.")

        trace_id = trace_id or uuid4()

        with self.lock:
            self._trace_counter = getattr(self, "_trace_counter", 0) + 1
            trace_ts = time.time() + (self._trace_counter * 1e-7)

        trace = TraceRecord(
            trace_id=trace_id,
            session_id=self.current_session_id,
            step_id=step_id,
            parent_trace_id=parent_trace_id,
            type=type,
            name=name,
            timestamp=trace_ts,
            duration=duration,
            status=status,
            payload=payload,
        )

        # Run storage in background
        try:
            asyncio.get_running_loop()
            task = asyncio.create_task(asyncio.to_thread(self.storage.create_trace, trace))
            self._pending_tasks.add(task)
            task.add_done_callback(self._on_background_task_done)
        except RuntimeError:
            # Fallback if no event loop
            def run_and_cleanup():
                self.storage.create_trace(trace)
                curr_thread = threading.current_thread()
                if curr_thread in self._pending_threads:
                    self._pending_threads.remove(curr_thread)

            thread = threading.Thread(target=run_and_cleanup)
            self._pending_threads.append(thread)
            thread.start()

        # Publish event
        self._publish("trace_recorded", trace.model_dump())

        return trace_id

    def record_failed_output(
        self,
        trace_id: UUID,
        model_name: str,
        prompt: str,
        raw_output: str,
        error_message: str,
    ):
        """Record a malformed output (crime scene) for later training."""
        if not self.current_session_id:
            return

        record = FailedOutputRecord(
            session_id=self.current_session_id,
            trace_id=trace_id,
            model_name=model_name,
            prompt=prompt,
            raw_output=raw_output,
            error_message=error_message,
        )

        try:
            task = asyncio.create_task(asyncio.to_thread(self.storage.create_failed_output, record))
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)
        except RuntimeError:

            def run_and_cleanup():
                self.storage.create_failed_output(record)
                curr_thread = threading.current_thread()
                if curr_thread in self._pending_threads:
                    self._pending_threads.remove(curr_thread)

            thread = threading.Thread(target=run_and_cleanup)
            self._pending_threads.append(thread)
            thread.start()

    def has_pending_operations(self) -> bool:
        """Check if there are any pending background tasks or threads."""
        return (
            len(self._pending_tasks) > 0
            or len([t for t in self._pending_threads if t.is_alive()]) > 0
        )

    async def shutdown(self):
        """Wait for all pending background tasks and threads to complete."""
        if self._pending_tasks:
            logger.info(
                f"Waiting for {len(self._pending_tasks)} pending async tasks to complete..."
            )
            await asyncio.gather(*self._pending_tasks, return_exceptions=True)

        if self._pending_threads:
            logger.info(f"Waiting for {len(self._pending_threads)} pending threads to complete...")
            threads_to_join = list(self._pending_threads)
            for thread in threads_to_join:
                if thread.is_alive():
                    await asyncio.to_thread(thread.join)

        if getattr(self, "ipc_socket", None):
            try:
                self.ipc_socket.close()
            except Exception:
                pass

        logger.info("DataEngine shutdown complete. All data persisted.")

    def stream_output(
        self,
        execution_id: UUID,
        chunk: str,
        stream_type: str | None = None,
        is_thinking: bool | None = None,
    ):
        """Stream LLM output chunks."""
        if stream_type is None:
            if is_thinking is True:
                stream_type = "thinking"
            else:
                stream_type = "text"

        parent_id = CURRENT_TRACE_ID.get()
        session_id_str = str(self.current_session_id) if self.current_session_id else None

        self._publish(
            "llm_stream",
            {
                "execution_id": str(execution_id),
                "session_id": session_id_str,
                "parent_trace_id": str(parent_id) if parent_id else None,
                "step_id": (str(self.current_step_id) if self.current_step_id else None),
                "chunk": chunk,
                "stream_type": stream_type,
                "is_thinking": (
                    is_thinking if is_thinking is not None else (stream_type == "thinking")
                ),
            },
        )

        exec_str = str(execution_id)
        if not hasattr(self, "_accumulated_logs"):
            self._accumulated_logs = {}
        if exec_str not in self._accumulated_logs:
            self._accumulated_logs[exec_str] = ""
        self._accumulated_logs[exec_str] += chunk

        if session_id_str:
            if session_id_str not in self._accumulated_logs:
                self._accumulated_logs[session_id_str] = ""
            self._accumulated_logs[session_id_str] += chunk

    def _get_generic_tools_for_sse(self, step_id: UUID) -> list[dict[str, Any]]:
        try:
            traces = self.storage.get_step_traces(step_id)
            return [t.model_dump() for t in traces if t.type == "tool"]
        except Exception as e:
            logger.error(f"Failed to fetch step traces for SSE: {e}")
            return []

    def _get_step_token_usage_for_sse(self, step_id: UUID) -> dict[str, Any] | None:
        try:
            traces = self.storage.get_step_traces(step_id)
            step_p, step_c, step_t = 0, 0, 0
            for t in traces:
                if t.type == "llm_call" and isinstance(t.payload, dict):
                    u = t.payload.get("token_usage") or t.payload.get("usage_metadata")
                    if isinstance(u, dict):
                        p = int(
                            u.get("prompt_tokens")
                            or u.get("prompt_token_count")
                            or u.get("input_tokens")
                            or 0
                        )
                        c = int(
                            u.get("completion_tokens")
                            or u.get("candidates_token_count")
                            or u.get("output_tokens")
                            or 0
                        )
                        tot = int(u.get("total_tokens") or u.get("total_token_count") or (p + c))
                        step_p += p
                        step_c += c
                        step_t += tot
            if step_t > 0:
                return {
                    "prompt_tokens": step_p,
                    "completion_tokens": step_c,
                    "total_tokens": step_t,
                }
        except Exception:
            pass
        return None

    def _on_background_task_done(self, task: asyncio.Task):
        self._pending_tasks.discard(task)
        if not task.cancelled() and task.exception():
            exc = task.exception()
            logger.error(f"Background task failed with exception: {exc}", exc_info=exc)
            raise exc

    def _run_in_background(self, fn, *args):
        """Safely offload blocking storage/disk operation to background without stalling event loop."""
        try:
            task = asyncio.create_task(asyncio.to_thread(fn, *args))
            self._pending_tasks.add(task)
            task.add_done_callback(self._on_background_task_done)
        except RuntimeError:

            def run_and_cleanup():
                try:
                    fn(*args)
                finally:
                    curr_thread = threading.current_thread()
                    if curr_thread in self._pending_threads:
                        self._pending_threads.remove(curr_thread)

            thread = threading.Thread(target=run_and_cleanup)
            self._pending_threads.append(thread)
            thread.start()

    def update_step_action(
        self,
        action_taken: dict[str, Any],
        post_screenshot_bytes: bytes | None = None,
    ):
        """Update the current step with action taken and post-action screenshot in background."""
        with self.lock:
            step_id = self.current_step_id or self.last_recorded_step_id
            if not step_id:
                raise ValueError("No active step to update.")
            if self.base_dir:
                step_dir = self.base_dir / "steps" / str(step_id)
                self.current_step_dir = step_dir
                step_dir.mkdir(parents=True, exist_ok=True)
            else:
                step_dir = None

        def _update_and_write():
            self.storage.update_step_action(step_id, action_taken)
            if post_screenshot_bytes and step_dir:
                step_dir.mkdir(parents=True, exist_ok=True)
                with open(step_dir / "post.jpg", "wb") as f:
                    f.write(post_screenshot_bytes)

        self._run_in_background(_update_and_write)

        update_payload = {
            "step_id": str(step_id),
            "action_taken": action_taken,
            "generic_tools": self._get_generic_tools_for_sse(step_id),
        }
        tokens = self._get_step_token_usage_for_sse(step_id)
        if tokens:
            update_payload["token_usage"] = tokens
            update_payload["total_tokens"] = tokens["total_tokens"]

        self._publish("step_updated", update_payload)

    def update_step_summary(self, step_id: UUID | int | str, summary: str):
        """Update the step with a concise summary in background."""
        target_uuid = None
        if isinstance(step_id, int):
            if self.current_session_id:
                steps = self.storage.get_steps(self.current_session_id)
                for s in steps:
                    if s.step_number == step_id:
                        target_uuid = s.step_id
                        break
        elif isinstance(step_id, str):
            try:
                target_uuid = UUID(step_id)
            except ValueError:
                target_uuid = step_id
        else:
            target_uuid = step_id

        if not target_uuid:
            logger.debug(f"Could not resolve step_id for {step_id} to update summary.")
            return

        self._run_in_background(self.storage.update_step_summary, target_uuid, summary)
        self._publish(
            "step_updated",
            {
                "step_id": str(target_uuid),
                "summary": summary,
                "generic_tools": self._get_generic_tools_for_sse(target_uuid),
            },
        )

    def update_step_thinking(self, step_id: UUID, operator_raw_thinking: str):
        """Update step's thinking in SQLite in background."""
        self._run_in_background(self.storage.update_step_thinking, step_id, operator_raw_thinking)
        self._publish(
            "step_updated",
            {
                "step_id": str(step_id),
                "operator_raw_thinking": operator_raw_thinking,
                "generic_tools": self._get_generic_tools_for_sse(step_id),
            },
        )

    def update_step_native_thinking(self, step_id: UUID, operator_native_thinking: str):
        """Update step's native thinking in SQLite in background."""
        self._run_in_background(
            self.storage.update_step_native_thinking,
            step_id,
            operator_native_thinking,
        )
        self._publish(
            "step_updated",
            {
                "step_id": str(step_id),
                "operator_native_thinking": operator_native_thinking,
                "generic_tools": self._get_generic_tools_for_sse(step_id),
            },
        )

    def update_step_execution_result(
        self,
        step_id: UUID,
        last_execution_result: dict,
        post_image_name: str | None = None,
    ):
        """Update step's execution result and post_image_name in SQLite in background."""
        self._run_in_background(
            self.storage.update_step_execution_result,
            step_id,
            last_execution_result,
            post_image_name,
        )
        self._publish(
            "step_updated",
            {
                "step_id": str(step_id),
                "last_execution_result": last_execution_result,
                "post_image_name": post_image_name,
                "generic_tools": self._get_generic_tools_for_sse(step_id),
            },
        )

    def get_relative_time(self, timestamp: float) -> str:
        """Compute relative time since session start."""
        if self.session_start_time is None:
            return "0.0s"
        diff = timestamp - self.session_start_time
        return f"{diff:.1f}s"

    def allocate_step_id(self) -> UUID:
        """Pre-allocate a step ID for the upcoming step to synchronize traces."""
        self.current_step_id = uuid4()
        if self.base_dir:
            self.current_step_dir = self.base_dir / "steps" / str(self.current_step_id)
            self.current_step_dir.mkdir(parents=True, exist_ok=True)
        return self.current_step_id

    def get_agent_friendly_steps(self) -> list[dict[str, Any]]:
        """Retrieve steps formatted for agent consumption (relative time)."""
        if not self.current_session_id:
            return []
        steps_with_traces = self.storage.get_steps_with_traces(self.current_session_id)
        result = []
        for step, traces in steps_with_traces:
            step_dict = step.model_dump()
            step_dict["relative_time"] = self.get_relative_time(step.timestamp)

            interleaved_events = []
            try:
                blacklist = (
                    "operator",
                    "perception",
                    "planner",
                    "validator",
                    "summarizer",
                    "checker",
                )

                relevant_traces = []
                for t in traces:
                    if t.status not in ("success", "failed"):
                        continue

                    is_relevant_llm = False
                    if t.type == "llm_call":
                        curr_parent = t.parent_trace_id
                        visited = set()
                        while curr_parent:
                            if curr_parent in visited:
                                break
                            visited.add(curr_parent)
                            parent_trace = next(
                                (x for x in traces if x.trace_id == curr_parent),
                                None,
                            )
                            if parent_trace:
                                if parent_trace.name in (
                                    "operator",
                                    "failure_analyzer",
                                ):
                                    is_relevant_llm = True
                                    t.name = parent_trace.name
                                    break
                                curr_parent = parent_trace.parent_trace_id
                            else:
                                break

                    if is_relevant_llm:
                        relevant_traces.append(t)
                    elif t.type in ("tool", "agent") and t.name not in blacklist:
                        relevant_traces.append(t)

                # Sort by timestamp to ensure exact chronological order
                relevant_traces.sort(key=lambda x: x.timestamp)

                # Separate set of candidate tool/agent IDs to do top-level filtering
                # (only keep top-level tool calls)
                candidate_ids = {t.trace_id for t in relevant_traces if t.type in ("tool", "agent")}

                for t in relevant_traces:
                    if t.type == "llm_call":
                        payload = t.payload or {}
                        response_list = payload.get("response") or []
                        thought_text = ""
                        has_structured_blocks = False
                        if response_list:
                            first_gen = response_list[0]
                            if isinstance(first_gen, dict):
                                content_val = first_gen.get("content")
                                if isinstance(content_val, list):
                                    has_structured_blocks = True
                                    for block in content_val:
                                        if isinstance(block, dict):
                                            if (
                                                block.get("type") == "thinking"
                                                and block.get("thinking", "").strip()
                                            ):
                                                e_type = (
                                                    "failure_analyzer_native_thought"
                                                    if t.name == "failure_analyzer"
                                                    else "native_thought"
                                                )
                                                interleaved_events.append(
                                                    {
                                                        "type": e_type,
                                                        "content": (block["thinking"].strip()),
                                                    }
                                                )
                                            elif (
                                                block.get("type") == "text"
                                                and block.get("text", "").strip()
                                            ):
                                                e_type = (
                                                    "failure_analyzer_thought"
                                                    if t.name == "failure_analyzer"
                                                    else "thought"
                                                )
                                                interleaved_events.append(
                                                    {
                                                        "type": e_type,
                                                        "content": (block["text"].strip()),
                                                    }
                                                )
                                if not has_structured_blocks:
                                    thought_text = (
                                        first_gen.get("content") or first_gen.get("text") or ""
                                    )
                            else:
                                thought_text = str(first_gen)

                        if not has_structured_blocks:
                            if isinstance(thought_text, list):
                                thought_text = safe_extract_text(thought_text)

                            if thought_text and thought_text.strip():
                                e_type = (
                                    "failure_analyzer_thought"
                                    if t.name == "failure_analyzer"
                                    else "thought"
                                )
                                interleaved_events.append(
                                    {
                                        "type": e_type,
                                        "content": thought_text.strip(),
                                    }
                                )
                    elif t.type in ("tool", "agent"):
                        # Keep only top-level candidate traces (no ancestor in candidates)
                        is_sub_call = False
                        curr_parent = t.parent_trace_id
                        visited = set()
                        while curr_parent:
                            if curr_parent in visited:
                                break
                            visited.add(curr_parent)
                            if curr_parent in candidate_ids:
                                is_sub_call = True
                                break
                            parent_trace = next(
                                (x for x in traces if x.trace_id == curr_parent),
                                None,
                            )
                            if parent_trace:
                                curr_parent = parent_trace.parent_trace_id
                            else:
                                break
                        if is_sub_call:
                            continue

                        payload = t.payload or {}
                        tc_args = payload.get("args") or {}

                        if isinstance(tc_args, dict):
                            filtered_args = {
                                k: v
                                for k, v in tc_args.items()
                                if k not in ("state", "tool_call_id")
                            }
                        else:
                            filtered_args = tc_args

                        tc_result = payload.get("result") or payload.get("error") or "No result"

                        interleaved_events.append(
                            {
                                "type": "tool_call",
                                "name": t.name,
                                "args": filtered_args,
                                "result": tc_result,
                            }
                        )

                step_dict["interleaved_events"] = interleaved_events
                step_dict["tool_calls"] = [
                    e for e in interleaved_events if e["type"] == "tool_call"
                ]
            except Exception as e:
                logger.error(f"Failed to retrieve traces for step {step.step_id}: {e}")
                step_dict["interleaved_events"] = []
                step_dict["tool_calls"] = []

            # Normalize physical coordinates to normalized ones for agent consumption
            try:
                extra = step_dict.get("extra_metadata") or {}
                width = extra.get("width") or (
                    getattr(self.ctx.device, "device_width", 1080)
                    if self.ctx and self.ctx.device
                    else 1080
                )
                height = extra.get("height") or (
                    getattr(self.ctx.device, "device_height", 2400)
                    if self.ctx and self.ctx.device
                    else 2400
                )

                if step_dict.get("action_taken"):
                    step_dict["action_taken"] = normalize_any_structure(
                        step_dict["action_taken"], width, height
                    )
                if step_dict.get("last_execution_result"):
                    step_dict["last_execution_result"] = normalize_any_structure(
                        step_dict["last_execution_result"], width, height
                    )
            except Exception as norm_err:
                logger.error(
                    f"Failed to normalize coordinates in friendly steps history: {norm_err}"
                )

            result.append(step_dict)
        return result

    def register_background_task(self, task_id: str, summary: str, trace_id: UUID | None = None):
        if not self.current_session_id:
            return

        record = BackgroundTaskRecord(
            task_id=task_id,
            session_id=self.current_session_id,
            summary=summary,
            status="running",
            start_time=time.time(),
            trace_id=str(trace_id) if trace_id else None,
        )

        if trace_id:
            if not hasattr(self, "_bg_task_to_trace_id"):
                self._bg_task_to_trace_id = {}
            self._bg_task_to_trace_id[task_id] = str(trace_id)

        try:
            task = asyncio.create_task(
                asyncio.to_thread(self.storage.create_background_task, record)
            )
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)
        except RuntimeError:

            def run_and_cleanup():
                self.storage.create_background_task(record)
                curr_thread = threading.current_thread()
                if curr_thread in self._pending_threads:
                    self._pending_threads.remove(curr_thread)

            thread = threading.Thread(target=run_and_cleanup)
            self._pending_threads.append(thread)
            thread.start()

        self._publish("background_tasks_updated", self.get_all_background_tasks())

    def unregister_background_task(self, task_id: str, status: str = "completed"):
        end_time = time.time()

        trace_id_str = None
        if hasattr(self, "_bg_task_to_trace_id"):
            trace_id_str = self._bg_task_to_trace_id.pop(task_id, None)

        logs = ""
        if trace_id_str and hasattr(self, "_accumulated_logs"):
            logs = self._accumulated_logs.pop(trace_id_str, "")

        try:
            task = asyncio.create_task(
                asyncio.to_thread(
                    self.storage.update_background_task_status_and_logs,
                    task_id,
                    status,
                    end_time,
                    logs,
                )
            )
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)
        except RuntimeError:

            def run_and_cleanup():
                self.storage.update_background_task_status_and_logs(task_id, status, end_time, logs)
                curr_thread = threading.current_thread()
                if curr_thread in self._pending_threads:
                    self._pending_threads.remove(curr_thread)

            thread = threading.Thread(target=run_and_cleanup)
            self._pending_threads.append(thread)
            thread.start()

        self._publish("background_tasks_updated", self.get_all_background_tasks())

    def get_all_background_tasks(self) -> list[dict]:
        if not self.current_session_id:
            return []
        records = self.storage.get_background_tasks(self.current_session_id)
        return [r.model_dump() for r in records]

    def get_active_background_tasks(self) -> list[dict]:
        return self.get_all_background_tasks()
