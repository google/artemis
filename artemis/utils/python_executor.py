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

import ast
import logging
from pathlib import Path
import queue
import subprocess
import time
from typing import IO

import jupyter_client
from jupyter_client.blocking import BlockingKernelClient
from zmq import ZMQError

logger = logging.getLogger(__name__)

# Allow extra startup time when the host is busy.
KERNEL_STARTUP_TIMEOUT_S = 60.0
EXECUTION_TIMEOUT_S = 120.0
# Poll frequently enough to detect a dead kernel or an expired deadline.
IOPUB_POLL_INTERVAL_S = 1.0
INTERRUPT_DRAIN_TIMEOUT_S = 5.0


def validate_coder_script(code_str: str) -> bool:
    """Reject resize() calls that bypass canvas transformation tracking."""
    try:
        tree = ast.parse(code_str)
    except SyntaxError as e:
        raise ValueError(f"Syntax error in code: {e}") from e

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "resize":
            raise ValueError(
                "SECURITY/VALIDATION ERROR: Direct usage of resize() is"
                " forbidden. You MUST use canvas.resize_by_factor(factor)."
            )

    return True


class PythonExecutor:
    """Run snippets in a dedicated Jupyter kernel with execution deadlines.

    Kernel output goes to a session log or DEVNULL so a lingering kernel
    cannot keep the parent's output pipes open.
    """

    def __init__(self, session_dir: Path | None = None) -> None:
        self.session_dir = Path(session_dir) if session_dir is not None else None
        self.km: jupyter_client.KernelManager | None = None
        self.kc: BlockingKernelClient | None = None
        self._kernel_log: IO[bytes] | None = None
        self._start()

    def _start(self) -> None:
        try:
            if self.session_dir is not None:
                self.session_dir.mkdir(parents=True, exist_ok=True)
                self._kernel_log = (self.session_dir / "kernel.log").open("ab")
            kernel_output = self._kernel_log if self._kernel_log is not None else subprocess.DEVNULL

            self.km = jupyter_client.KernelManager(kernel_name="python3")
            self.km.start_kernel(stdout=kernel_output, stderr=kernel_output)
            self.kc = self.km.client()
            self.kc.start_channels()
            self.kc.wait_for_ready(timeout=KERNEL_STARTUP_TIMEOUT_S)
        except Exception as e:
            logger.error("Failed to start Jupyter kernel: %s", e)
            # Startup may fail after creating the process or opening the log.
            self.close()
            raise RuntimeError(f"Kernel startup failed: {e}") from e

    def _restart(self) -> None:
        """Replace an unresponsive kernel so the next cell can execute.

        Image intermediates are stored on disk and survive the restart.
        """
        logger.warning("Restarting Python kernel after an execution timeout")
        self.close()
        self._start()

    def _kernel_alive(self) -> bool:
        if self.km is None or self.kc is None:
            return False
        try:
            return bool(self.km.is_alive())
        except (OSError, RuntimeError) as e:
            logger.error("Failed to query kernel liveness: %s", e)
            return False

    def _interrupt_and_drain(self, msg_id: str) -> bool:
        """Return whether the interrupted request becomes idle before the deadline.

        Restart the kernel if it does not acknowledge the interrupt.
        """
        assert self.km is not None and self.kc is not None
        try:
            self.km.interrupt_kernel()
        except (OSError, RuntimeError, ZMQError) as e:
            logger.error("Failed to interrupt kernel after timeout: %s", e)
            return False
        deadline = time.monotonic() + INTERRUPT_DRAIN_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                msg = self.kc.get_iopub_msg(timeout=IOPUB_POLL_INTERVAL_S)
            except queue.Empty:
                if not self._kernel_alive():
                    return False
                continue
            if (
                msg["parent_header"].get("msg_id") == msg_id
                and msg["header"]["msg_type"] == "status"
                and msg["content"].get("execution_state") == "idle"
            ):
                return True
        return False

    def execute(self, code: str) -> str:
        try:
            validate_coder_script(code)
        except ValueError as ve:
            return str(ve)

        if not self._kernel_alive():
            return "[Kernel Error] The Python kernel is not running."
        assert self.kc is not None

        msg_id = self.kc.execute(code)
        deadline = time.monotonic() + EXECUTION_TIMEOUT_S
        output_text = []

        while True:
            # Check before reading: a cell may produce output continuously.
            if time.monotonic() >= deadline:
                output_text.append(
                    "[Execution Timeout Error] Execution exceeded"
                    f" {EXECUTION_TIMEOUT_S:.0f}s and was stopped."
                )
                if not self._interrupt_and_drain(msg_id):
                    try:
                        self._restart()
                    except RuntimeError as e:
                        output_text.append(f"\n[Kernel Error] {e}")
                break

            try:
                msg = self.kc.get_iopub_msg(timeout=IOPUB_POLL_INTERVAL_S)
            except queue.Empty:
                if not self._kernel_alive():
                    output_text.append(
                        "[Kernel Error] The Python kernel died while executing the code."
                    )
                    break
                continue

            # Only consume messages that belong to this request; anything else
            # is a leftover from kernel startup or an earlier interrupted cell.
            if msg["parent_header"].get("msg_id") != msg_id:
                continue

            msg_type = msg["header"]["msg_type"]
            if msg_type == "stream":
                output_text.append(msg["content"]["text"])
            elif msg_type == "error":
                traceback = "\n".join(msg["content"]["traceback"])
                output_text.append(f"Traceback:\n{traceback}")
            elif msg_type in ("display_data", "execute_result"):
                data = msg["content"]["data"]
                if "text/plain" in data:
                    output_text.append(data["text/plain"])
            elif msg_type == "status" and msg["content"]["execution_state"] == "idle":
                break

        return "".join(output_text)

    def close(self) -> None:
        kc, km, kernel_log = self.kc, self.km, self._kernel_log
        self.kc = None
        self.km = None
        self._kernel_log = None

        if kc is not None:
            try:
                kc.stop_channels()
            except (OSError, RuntimeError, ZMQError) as e:
                logger.error("Failed to stop kernel channels: %s", e)

        if km is not None:
            try:
                if km.has_kernel:
                    # Do not wait for a busy cell to finish before shutting down.
                    km.shutdown_kernel(now=True)
            except Exception as e:
                logger.error("Failed to shut down kernel: %s", e)

        if kernel_log is not None:
            try:
                kernel_log.close()
            except OSError as e:
                logger.debug("Failed to close kernel log: %s", e)
