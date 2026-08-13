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

import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import traceback
from typing import Any
from uuid import UUID

WORKSPACE_ROOT = Path(__file__).parent.parent.resolve()

# Try to import settings from artemis
try:
    from artemis.config import settings

    TRACES_PATH = Path(settings.TRACES_PATH)
except ImportError:
    # Fallback to default path if run outside project environment or imports fail
    TRACES_PATH = WORKSPACE_ROOT / "traces"

DB_PATH = TRACES_PATH / "data_engine.db"
IMAGES_DIR = TRACES_PATH / "images"

try:
    from artemis.context import ArtemisContext
    from artemis.graph.state import State
except ImportError:
    ArtemisContext = Any
    State = Any

REPLAY_TOOLS_CONFIG = {
    "ask_explorer": {
        "display_name": "Ask Explorer",
        "description": (
            "Visual parsing agent that executes visual ReAct search loops using multi-modal models."
        ),
        "module": "artemis.tools.explorer_tool",
        "function": "_run_explorer_logic",
        "agent_name": "explorer",
        "denylist_args": ["ctx", "state"],
        "fallback_mappings": {},
        "llm_span_name": "gemini_explorer_call",
    },
    "ask_image_processor": {
        "display_name": "Ask Image Processor",
        "description": (
            "Visual parsing agent that writes Python code using OpenCV and"
            " computer vision to analyze and modify screen images."
        ),
        "module": "artemis.tools.image_processor_tool",
        "function": "_run_image_processor_logic",
        "agent_name": "image_processor",
        "denylist_args": ["ctx", "state"],
        "fallback_mappings": {},
        "llm_span_name": "gemini_image_processor_call",
    },
    # Future tools can easily be registered here using plain dictionaries:
    # "ask_validator": {
    #     "display_name": "Ask Validator",
    #     "description": "Validation agent that checks if a step or action was completed successfully.",
    #     "module": "artemis.tools.validator_tool",
    #     ...
    # }
}


class ReplayManager:
    """Manages sandboxed execution and diagnostics of visual agent steps.

    The ReplayManager is the core orchestrator for the Artemis step replay system.
    It reconstructs historical execution states (SQLite database records, trace
    histories, and screenshots) and replays a single agent step in an isolated,
    deterministic sandbox environment while interacting live with a physical
    Android device and Gemini.

    Key Responsibilities:
        - Reconstructs and preloads historical SQLite states up to a specific
        step.
        - Establishes sandboxed `ArtemisContext` and `DataEngine` instances.
        - Integrates with physical devices via ADB and UIAutomator.
        - Hooks and spies on the Gemini API to capture thoughts and raw outputs.
        - Reconciles live and preloaded traces into unified chronological trees.

    Attributes:
        workspace_root (Path): The absolute path to the root of the project
          workspace.
        replay_base_dir (Path): Base directory containing replay data and
          outputs.
        test_data_dir (Path): Directory containing chunked historical step
          traces.
        test_outputs_dir (Path): Sandbox directory where replay outputs are
          written.
        db_path (Path): Path to the master Data Engine SQLite database.
        traces_path (Path): Path to the master traces directory.
        images_dir (Path): Path to the master images directory.
    """

    def __init__(
        self,
        workspace_root: Path = WORKSPACE_ROOT,
        device_id: str = None,
        original_db_path: Path = None,
        init_device: bool = False,
    ):
        """Initializes the ReplayManager and autodetects the connected Android device.

        Args:
            workspace_root (Path, optional): The absolute path to the workspace
              root. Defaults to WORKSPACE_ROOT.
            device_id (str, optional): The serial ID of the target Android
              device. If not specified, autodetects the first connected device.
            original_db_path (Path, optional): Path to the master SQLite
              database. Defaults to DB_PATH.
            init_device (bool, optional): Whether to initialize the ADB device
              connection on startup.
        """
        self.workspace_root = workspace_root
        self.traces_path = TRACES_PATH
        self.replay_base_dir = self.traces_path / "replay"
        self.test_data_dir = self.replay_base_dir / "data"
        self.test_outputs_dir = self.replay_base_dir / "outputs"
        self.db_path = Path(original_db_path) if original_db_path else DB_PATH
        self.images_dir = IMAGES_DIR

        # Device connection and autodetection
        self.device_id = None
        self.w = None
        self.h = None
        self.adb = None
        self.ui_client = None

        if init_device:
            try:
                self._init_device(device_id)
            except Exception as e:
                print(
                    "Warning: Connected device could not be initialized during"
                    f" ReplayManager startup: {e}"
                )

    def _init_device(self, device_id: str = None):
        """Initializes connection to the target device and queries screen metrics."""
        from adbutils import AdbClient

        try:
            from artemis.clients.ui_automator_client import UIAutomatorClient
        except ImportError:
            raise ImportError(
                "Failed to import UIAutomatorClient. Ensure artemis package is installed in path."
            )

        self.adb = AdbClient(host="localhost", port=5037)
        try:
            devices = self.adb.device_list()
        except Exception as adb_err:
            raise ConnectionError(
                f"Failed to query device list from ADB server: {adb_err}"
            ) from adb_err

        if not devices:
            raise ConnectionError(
                "No active ADB devices connected. Please connect an Android device via ADB."
            )

        if device_id:
            matched_device = next((d.serial for d in devices if d.serial == device_id), None)
            if not matched_device:
                raise ConnectionError(
                    f"Requested device '{device_id}' is not connected."
                    f" Connected devices: {[d.serial for d in devices]}"
                )
            self.device_id = matched_device
        else:
            self.device_id = devices[0].serial

        self.ui_client = UIAutomatorClient(device_id=self.device_id)
        ui_data = self.ui_client.get_screen_data()
        self.w, self.h = ui_data.width, ui_data.height
        print(f"Connected to device: {self.device_id} ({self.w}x{self.h})")

    def chunk_session_traces(self, session_id: str, output_dir: Path = None) -> Path:
        """Chunks traces, screenshots, and metadata for a specific execution session.

        Queries the master SQLite database and exports step-by-step
        subdirectories
        (e.g., `step_00/`, `step_01/`) inside the output directory.

        Args:
            session_id: The UUID string of the session to extract and chunk.
            output_dir: Optional path to the directory where chunked results
              should be saved. Defaults to `self.test_data_dir /
              f"{session_id}_chunked"`.
        """
        import sqlite3
        import json
        import shutil

        db_path = self.db_path
        traces_dir = self.traces_path

        if not db_path.exists():
            raise FileNotFoundError(f"Master database not found at {db_path}")

        if not output_dir:
            output_dir = self.test_data_dir / f"{session_id}_chunked"
        else:
            output_dir = Path(output_dir)

        if output_dir.exists() or output_dir.is_symlink():
            # Safety check: only delete directories that are named *_chunked
            if output_dir.name.endswith("_chunked"):
                if output_dir.is_symlink():
                    output_dir.unlink()
                elif (
                    self.test_data_dir in output_dir.parents
                    or output_dir.parent == self.test_data_dir
                    or output_dir.parent.name == "inputs"
                ):
                    shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        print(f"Chunking traces for session: {session_id}")
        print(f"Using database: {db_path}")
        print(f"Output directory: {output_dir}")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
        session_row = cursor.fetchone()

        cursor.execute(
            "SELECT * FROM steps WHERE session_id = ? ORDER BY step_number ASC",
            (session_id,),
        )
        steps = cursor.fetchall()
        print(f"Found {len(steps)} steps for session {session_id}")

        if not session_row and not steps:
            conn.close()
            raise ValueError(f"Session {session_id} not found in database.")

        for step in steps:
            step_number = step["step_number"]
            step_id = step["step_id"]
            step_dir = output_dir / f"step_{step_number:02d}"
            step_dir.mkdir(parents=True, exist_ok=True)

            # 1. Process and save the step's primary metadata (step.json)
            step_data = {}
            for key in step.keys():
                val = step[key]
                if (
                    key
                    in (
                        "action_taken",
                        "last_execution_result",
                        "extra_metadata",
                    )
                    and val
                ):
                    try:
                        step_data[key] = json.loads(val)
                    except Exception:
                        step_data[key] = val
                else:
                    step_data[key] = val

            step_json_path = step_dir / "step.json"
            with open(step_json_path, "w", encoding="utf-8") as f:
                json.dump(step_data, f, indent=2, ensure_ascii=False)

            # 2. Retrieve and save trace logs recorded during this step (traces.json)
            cursor.execute(
                "SELECT * FROM traces WHERE step_id = ? ORDER BY timestamp ASC",
                (step_id,),
            )
            traces = cursor.fetchall()

            traces_data = []
            for trace_row in traces:
                trace_dict = {}
                for key in trace_row.keys():
                    val = trace_row[key]
                    if key == "payload" and val:
                        try:
                            trace_dict[key] = json.loads(val)
                        except Exception:
                            trace_dict[key] = val
                    else:
                        trace_dict[key] = val
                traces_data.append(trace_dict)

            traces_json_path = step_dir / "traces.json"
            with open(traces_json_path, "w", encoding="utf-8") as f:
                json.dump(traces_data, f, indent=2, ensure_ascii=False)

            # 3. Copy the step's screenshots (pre-action and post-action screenshots)
            pre_image_name = step_data.get("pre_image_name")
            post_image_name = step_data.get("post_image_name")
            if pre_image_name and post_image_name == pre_image_name:
                post_image_name = None
            images_dir = traces_dir / "images"

            if pre_image_name:
                src_pre = images_dir / f"{pre_image_name}.jpg"
                if src_pre.exists():
                    shutil.copy2(src_pre, step_dir / "pre.jpg")
                else:
                    print(f"Warning: Pre-action screenshot not found at {src_pre}")

            if post_image_name:
                src_post = images_dir / f"{post_image_name}.jpg"
                if src_post.exists():
                    shutil.copy2(src_post, step_dir / "post.jpg")
                else:
                    print(f"Warning: Post-action screenshot not found at {src_post}")

            # 4. Save metadata of the pre-action screenshot (pre_image_meta.json)
            if pre_image_name:
                cursor.execute(
                    "SELECT * FROM images WHERE image_name = ?",
                    (pre_image_name,),
                )
                img_row = cursor.fetchone()
                if img_row:
                    img_meta = {}
                    for key in img_row.keys():
                        val = img_row[key]
                        if key in ("ocr_result", "ui_tree", "extra_metadata") and val:
                            try:
                                img_meta[key] = json.loads(val)
                            except Exception:
                                img_meta[key] = val
                        else:
                            img_meta[key] = val

                    pre_img_meta_path = step_dir / "pre_image_meta.json"
                    with open(pre_img_meta_path, "w", encoding="utf-8") as f:
                        json.dump(img_meta, f, indent=2, ensure_ascii=False)
                else:
                    print(
                        f"Warning: Image metadata row for {pre_image_name} not"
                        " found in images table."
                    )

            # 5. Save metadata of the post-action screenshot (post_image_meta.json)
            if post_image_name:
                cursor.execute(
                    "SELECT * FROM images WHERE image_name = ?",
                    (post_image_name,),
                )
                img_row = cursor.fetchone()
                if img_row:
                    img_meta = {}
                    for key in img_row.keys():
                        val = img_row[key]
                        if key in ("ocr_result", "ui_tree", "extra_metadata") and val:
                            try:
                                img_meta[key] = json.loads(val)
                            except Exception:
                                img_meta[key] = val
                        else:
                            img_meta[key] = val

                    post_img_meta_path = step_dir / "post_image_meta.json"
                    with open(post_img_meta_path, "w", encoding="utf-8") as f:
                        json.dump(img_meta, f, indent=2, ensure_ascii=False)
                else:
                    print(
                        f"Warning: Image metadata row for {post_image_name} not"
                        " found in images table."
                    )

        conn.close()
        # Write sentinel file to indicate successful chunking
        try:
            (output_dir / ".chunked").touch()
        except Exception as e:
            print(f"Warning: Failed to write chunked sentinel file: {e}")
        print("Chunking trace completion status: success")
        return output_dir

    def list_devices(self) -> list[dict]:
        """Dynamically queries the ADB server for connected Android devices."""
        try:
            from adbutils import AdbClient

            adb = AdbClient(host="localhost", port=5037)
            devices = adb.device_list()
            return [{"serial": d.serial, "status": "online"} for d in devices]
        except Exception as e:
            print(f"Warning: Failed to query device list from ADB: {e}")
            return []

    def load_session_goal(
        self, session_id: str, step_dir: Path, original_db_path: str = None
    ) -> str:
        """Loads the session goal from either session.json or the original session db."""
        session_json_path = step_dir.parent / "session.json"
        if session_json_path.exists():
            try:
                with open(session_json_path, encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and "initial_goal" in data and data["initial_goal"]:
                        return data["initial_goal"]
            except Exception as e:
                print(f"Warning: Failed to load goal from session.json: {e}")

        if original_db_path and Path(original_db_path).exists():
            try:
                conn = sqlite3.connect(original_db_path)
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT initial_goal FROM sessions WHERE session_id = ?",
                    (str(session_id),),
                )
                row = cursor.fetchone()
                conn.close()
                if row and row["initial_goal"]:
                    return row["initial_goal"]
            except Exception as e:
                print(f"Warning: Failed to load goal from original database: {e}")

        raise ValueError(
            f"Could not load initial_goal for session {session_id} from"
            f" session.json or original database {original_db_path}."
        )

    def extract_replay_data(
        self,
        step_dir,
        original_db_path=None,
        agent_name="explorer",
        tool_name="ask_explorer",
    ):
        """Extracts final answer and chronological tool call sequence from traces.json/db."""
        step_json_path = step_dir / "step.json"
        if not step_json_path.exists():
            raise FileNotFoundError(f"step.json not found in {step_dir}")

        with open(step_json_path, encoding="utf-8") as f:
            step_data = json.load(f)
        step_id = step_data["step_id"]

        traces_json_path = step_dir / "traces.json"
        traces = []
        if traces_json_path.exists():
            with open(traces_json_path, encoding="utf-8") as f:
                traces = json.load(f)

        # If traces.json is empty/missing or lacks agent trace, check original DB
        agent_trace = next(
            (t for t in traces if t.get("type") == "agent" and t.get("name") == agent_name),
            None,
        )
        if (not agent_trace or not traces) and original_db_path and Path(original_db_path).exists():
            conn = sqlite3.connect(original_db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM traces WHERE step_id = ? ORDER BY timestamp ASC",
                (step_id,),
            )
            rows = cursor.fetchall()
            traces = []
            for r in rows:
                td = dict(r)
                if td.get("payload"):
                    try:
                        td["payload"] = json.loads(td["payload"])
                    except Exception:
                        pass
                traces.append(td)
            conn.close()
            agent_trace = next(
                (t for t in traces if t.get("type") == "agent" and t.get("name") == agent_name),
                None,
            )

        if not agent_trace:
            print(
                f"Warning: {agent_name} agent trace not found. Replay will"
                " fallback to direct submit_answer."
            )
            return (
                [],
                {
                    "candidates": [],
                    "fallback_message": f"No {agent_name} trace found.",
                },
                traces,
            )

        payload = agent_trace.get("payload") or {}
        result_str = payload.get("result", "{}")
        if isinstance(result_str, str):
            try:
                final_submit_args = json.loads(result_str)
            except Exception:
                final_submit_args = {
                    "candidates": [],
                    "fallback_message": result_str,
                }
        else:
            final_submit_args = result_str

        # Build sequence of intermediate tool calls under the agent trace
        agent_trace_id = agent_trace["trace_id"]

        def get_descendants(pid):
            child_list = []
            for t in traces:
                if t.get("parent_trace_id") == pid:
                    child_list.append(t)
                    child_list.extend(get_descendants(t["trace_id"]))
            return child_list

        descendants = get_descendants(agent_trace_id)
        descendants.sort(key=lambda x: x.get("timestamp", 0))

        tool_calls_sequence = []
        for t in descendants:
            if t.get("type") == "tool" and t.get("name") not in (
                tool_name,
                "submit_answer",
            ):
                t_payload = t.get("payload") or {}
                t_args = t_payload.get("args") or {}
                tool_calls_sequence.append({"name": t.get("name"), "args": t_args})

        # Preload traces up to the point when agent is called
        traces_to_preload = []
        if agent_trace in traces:
            idx = traces.index(agent_trace)
            traces_to_preload = traces[:idx]
        else:
            agent_ts = agent_trace.get("timestamp", 0.0)
            traces_to_preload = [t for t in traces if t.get("timestamp", 0.0) < agent_ts]

        return tool_calls_sequence, final_submit_args, traces_to_preload

    def resolve_step_data_path(self, data: dict, path: str):
        """Resolves a value from a nested dict/list using a dot-separated path (e.g.

        'action_taken.0.target_text').
        """
        parts = path.split(".")
        curr = data
        for p in parts:
            if isinstance(curr, dict):
                curr = curr.get(p)
            elif isinstance(curr, list):
                try:
                    idx = int(p)
                    if idx < len(curr):
                        curr = curr[idx]
                    else:
                        return None
                except ValueError:
                    return None
            else:
                return None
        return curr

    def setup_temporary_database(
        self,
        temp_db_path,
        temp_traces_dir,
        session_id,
        step_data,
        pre_image_meta,
        initial_goal,
        traces_to_preload=None,
        post_image_meta=None,
        replay_id=None,
    ):
        """Initializes a sandbox SQLite database with preloaded step and screen metadata."""
        if not initial_goal:
            raise ValueError("initial_goal must be provided and cannot be empty.")

        if temp_db_path.exists():
            try:
                temp_db_path.unlink()
            except OSError:
                pass

        if temp_traces_dir.exists():
            shutil.rmtree(temp_traces_dir)
        temp_traces_dir.mkdir(parents=True, exist_ok=True)

        try:
            from artemis.data_engine.storage import StorageManager
            from artemis.data_engine.models import (
                SessionMetadata,
                ImageRecord,
                StepRecord,
                TraceRecord,
            )
        except ImportError:
            raise ImportError(
                "Failed to import Artemis Data Engine modules. Ensure you are"
                " running in the correct virtual environment."
            )

        storage = StorageManager(temp_db_path, temp_traces_dir)

        # Preload session metadata
        session_meta = SessionMetadata(
            session_id=UUID(session_id) if isinstance(session_id, str) else session_id,
            initial_goal=initial_goal,
            status="running",
        )
        storage.create_session(session_meta)

        if replay_id:
            replay_session_meta = SessionMetadata(
                session_id=UUID(replay_id) if isinstance(replay_id, str) else replay_id,
                initial_goal=initial_goal,
                status="running",
            )
            storage.create_session(replay_session_meta)

        # Preload image metadata
        if pre_image_meta:
            image_record = ImageRecord(
                image_name=pre_image_meta["image_name"],
                timestamp=pre_image_meta.get("timestamp", 0.0),
                ocr_result=pre_image_meta.get("ocr_result"),
                ui_tree=pre_image_meta.get("ui_tree"),
                extra_metadata=pre_image_meta.get("extra_metadata", {}),
            )
            storage.create_image(image_record)

        if post_image_meta:
            image_record = ImageRecord(
                image_name=post_image_meta["image_name"],
                timestamp=post_image_meta.get("timestamp", 0.0),
                ocr_result=post_image_meta.get("ocr_result"),
                ui_tree=post_image_meta.get("ui_tree"),
                extra_metadata=post_image_meta.get("extra_metadata", {}),
            )
            storage.create_image(image_record)

        # Preload step metadata
        step_record = StepRecord(
            step_id=step_data["step_id"],
            session_id=step_data["session_id"],
            step_number=step_data["step_number"],
            timestamp=step_data.get("timestamp", 0.0),
            pre_image_name=step_data.get("pre_image_name"),
            post_image_name=step_data.get("post_image_name"),
            summary=step_data.get("summary"),
            action_taken=step_data.get("action_taken"),
            operator_raw_thinking=step_data.get("operator_raw_thinking"),
            operator_native_thinking=step_data.get("operator_native_thinking"),
            last_execution_result=step_data.get("last_execution_result"),
            extra_metadata=step_data.get("extra_metadata", {}),
        )
        storage.create_step(step_record)

        # Preload trace history
        if traces_to_preload:
            for t in traces_to_preload:
                try:
                    payload_val = t.get("payload")
                    if isinstance(payload_val, str):
                        try:
                            payload_val = json.loads(payload_val)
                        except Exception:
                            payload_val = {}
                    elif not isinstance(payload_val, dict):
                        payload_val = {}

                    trace_rec = TraceRecord(
                        trace_id=UUID(t["trace_id"])
                        if isinstance(t["trace_id"], str)
                        else t["trace_id"],
                        session_id=UUID(t["session_id"])
                        if isinstance(t["session_id"], str)
                        else t["session_id"],
                        step_id=UUID(t["step_id"]) if t.get("step_id") else None,
                        parent_trace_id=UUID(t["parent_trace_id"])
                        if t.get("parent_trace_id")
                        else None,
                        type=t["type"],
                        name=t["name"],
                        timestamp=t.get("timestamp", 0.0),
                        duration=t.get("duration"),
                        status=t.get("status", "success"),
                        payload=payload_val,
                    )
                    storage.create_trace(trace_rec)
                except Exception as ex:
                    print(f"Warning: Failed to preload trace {t.get('trace_id')}: {ex}")

        return storage

    def get_preloaded_trace_ids_for_step(self, session_id: str, step_num: int) -> set:
        traces_json_path = (
            self.test_data_dir / f"{session_id}_chunked" / f"step_{step_num:02d}" / "traces.json"
        )
        if traces_json_path.exists():
            try:
                with open(traces_json_path, encoding="utf-8") as f:
                    traces = json.load(f)
                return {str(t.get("trace_id")).lower() for t in traces if t.get("trace_id")}
            except Exception:
                pass
        return set()

    def load_traces_from_db(self, db_path: Path, step_num: int, preloaded_trace_ids: set) -> list:
        if not db_path.exists():
            return []
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT trace_id, session_id, step_id, parent_trace_id, type,"
                " name, status, timestamp, duration, payload FROM traces ORDER"
                " BY timestamp ASC"
            )
            rows = cursor.fetchall()
            conn.close()

            preloaded_set = {str(tid).lower() for tid in preloaded_trace_ids if tid}
            step_traces = []
            for r in rows:
                td = dict(r)
                if td.get("payload"):
                    try:
                        td["payload"] = json.loads(td["payload"])
                    except Exception:
                        pass
                if not isinstance(td.get("payload"), dict):
                    td["payload"] = {}

                tid = str(r["trace_id"]).lower()
                if tid in preloaded_set:
                    td["is_preloaded"] = True
                else:
                    td["is_preloaded"] = False

                td["step_number"] = step_num
                step_traces.append(td)
            return step_traces
        except Exception as e:
            print(
                f"Warning: Failed to load traces from database {db_path}: {e}",
                file=sys.stderr,
            )
            return []

    def normalize_trace_payload(self, t: dict) -> dict:
        if t.get("type") == "tool":
            payload = t.get("payload") or {}
            args = payload.get("args") or {}
            positional_args = payload.get("positional_args") or []

            if (not args or args == {}) and positional_args:
                name = t.get("name")
                if name == "search_by_coords" and len(positional_args) >= 2:
                    try:
                        args = {
                            "nx": int(positional_args[0]),
                            "ny": int(positional_args[1]),
                        }
                    except ValueError:
                        args = {
                            "nx": positional_args[0],
                            "ny": positional_args[1],
                        }
                elif name == "search_ui" and len(positional_args) >= 1:
                    args = {"search_query": positional_args[0]}
                elif name == "detect_objects" and len(positional_args) >= 1:
                    args = {"labels": positional_args[0]}
                else:
                    args = {"positional_args": positional_args}

                payload["args"] = args
                t["payload"] = payload
        return t

    def load_all_session_traces(
        self,
        temp_db_path: Path,
        preloaded_trace_ids: set,
        current_step_number: int,
    ) -> list:
        """Loads and merges all traces across all steps in the session."""
        session_id = None
        try:
            session_id = temp_db_path.parents[1].name.split("_")[0]
        except Exception:
            pass

        if not session_id:
            try:
                conn = sqlite3.connect(temp_db_path)
                cursor = conn.cursor()
                cursor.execute("SELECT session_id FROM traces LIMIT 1")
                row = cursor.fetchone()
                if row:
                    session_id = str(row[0])
                conn.close()
            except Exception:
                pass

        if not session_id:
            return self.load_traces_from_db(temp_db_path, current_step_number, preloaded_trace_ids)

        session_chunked_dir = self.test_data_dir / f"{session_id}_chunked"

        all_traces = []
        if session_chunked_dir.exists():
            step_subdirs = sorted(
                [
                    d
                    for d in session_chunked_dir.iterdir()
                    if d.is_dir() and d.name.startswith("step_")
                ],
                key=lambda d: d.name,
            )

            for subdir in step_subdirs:
                try:
                    step_num = int(subdir.name.split("_")[1])
                except (IndexError, ValueError):
                    continue

                step_outputs_dir = self.test_outputs_dir / f"{session_id}_step_{step_num:02d}"
                step_db_path = step_outputs_dir / "temp_traces" / "data_engine.db"

                step_preloaded_ids = self.get_preloaded_trace_ids_for_step(session_id, step_num)

                if step_db_path.exists():
                    step_traces = self.load_traces_from_db(
                        step_db_path, step_num, step_preloaded_ids
                    )
                    all_traces.extend(step_traces)
                else:
                    traces_json_path = subdir / "traces.json"
                    if traces_json_path.exists():
                        try:
                            with open(traces_json_path, encoding="utf-8") as f:
                                step_traces = json.load(f)
                            for t in step_traces:
                                t["is_preloaded"] = True
                                t["step_number"] = step_num
                                if not isinstance(t.get("payload"), dict):
                                    t["payload"] = {}
                                all_traces.append(t)
                        except Exception as e:
                            print(
                                f"Warning: Failed to load traces from {traces_json_path}: {e}",
                                file=sys.stderr,
                            )
        else:
            all_traces = self.load_traces_from_db(
                temp_db_path, current_step_number, preloaded_trace_ids
            )

        # Align live replay traces to the appropriate preloaded step/operator trace
        for step_num in range(1, 100):
            current_step_id = None
            current_step_dir = self.test_data_dir / f"{session_id}_chunked" / f"step_{step_num:02d}"
            if not current_step_dir.exists():
                continue

            step_json_path = current_step_dir / "step.json"
            if step_json_path.exists():
                try:
                    with open(step_json_path, encoding="utf-8") as f:
                        step_data = json.load(f)
                        current_step_id = step_data.get("step_id")
                except Exception:
                    pass

            current_step_id_str = str(current_step_id).lower() if current_step_id else None

            # 1. Find live root trace (no parent_trace_id and not preloaded)
            live_root_trace = None
            for t in all_traces:
                if t.get("step_number") != step_num:
                    continue
                if not t.get("is_preloaded") and not t.get("parent_trace_id"):
                    live_root_trace = t
                    break

            # 2. Find all preloaded traces for this step
            step_preloaded_traces = [
                t
                for t in all_traces
                if t.get("step_number") == step_num
                and t.get("is_preloaded")
                and str(t.get("step_id") or "").lower() == current_step_id_str
            ]

            # Sort chronologically to identify the final response of the step
            step_preloaded_traces.sort(key=lambda x: x.get("timestamp", 0.0))
            final_response_trace = step_preloaded_traces[-1] if step_preloaded_traces else None

            # 3. Branch immediately before the final response of the step
            if live_root_trace and final_response_trace:
                # Resolve the tool name from the live root trace name
                tool_name = "ask_explorer"
                agent_name = live_root_trace.get("name")
                for t_name, cfg in REPLAY_TOOLS_CONFIG.items():
                    if cfg.get("agent_name") == agent_name:
                        tool_name = t_name
                        break

                import uuid

                virtual_id = str(uuid.uuid4())

                # Branch from the same parent as the final response
                parent_id = final_response_trace.get("parent_trace_id")

                # Set timestamp to be immediately before the final response
                virtual_timestamp = final_response_trace.get("timestamp", 0.0) - 0.001

                live_query = None
                live_cf = None
                if live_root_trace.get("payload") and live_root_trace["payload"].get("args"):
                    args = live_root_trace["payload"]["args"]
                    live_query = (
                        args.get("q") or args.get("query") or args.get("arg2") or args.get("goal")
                    )
                    live_cf = (
                        args.get("cf") or args.get("context_feedback") or args.get("arg3") or ""
                    )
                if not live_query:
                    live_query = "Replay Execution"

                virtual_tool_trace = {
                    "trace_id": virtual_id,
                    "session_id": session_id,
                    "step_id": current_step_id,
                    "parent_trace_id": parent_id,
                    "type": "tool",
                    "name": tool_name,
                    "status": live_root_trace.get("status", "success"),
                    "timestamp": virtual_timestamp,
                    "duration": live_root_trace.get("duration", 0.0),
                    "payload": {
                        "args": {
                            "query": live_query,
                            "context_feedback": live_cf,
                        },
                        "result": (live_root_trace.get("payload", {}).get("result")),
                    },
                    "is_preloaded": False,
                    "step_number": step_num,
                }

                # Link the live root trace to our new virtual tool trace
                live_root_trace["parent_trace_id"] = virtual_id
                all_traces.append(virtual_tool_trace)

        all_traces = [self.normalize_trace_payload(t) for t in all_traces]
        all_traces.sort(key=lambda t: t.get("timestamp", 0.0))
        return all_traces

    def enrich_all_session_traces(self, all_traces: list, session_id: str) -> list:
        """Enriches all preloaded traces (across all steps) with file URLs to their corresponding screenshots."""
        session_chunked_dir = self.test_data_dir / f"{session_id}_chunked"

        step_dirs = {}
        if session_chunked_dir.exists():
            for subdir in session_chunked_dir.iterdir():
                if subdir.is_dir() and subdir.name.startswith("step_"):
                    step_json_path = subdir / "step.json"
                    if step_json_path.exists():
                        try:
                            with open(step_json_path, encoding="utf-8") as f:
                                step_data = json.load(f)
                                sid = step_data.get("step_id")
                                if sid:
                                    step_dirs[str(sid).lower()] = subdir
                        except Exception:
                            pass

        for td in all_traces:
            if td.get("is_preloaded"):
                step_id = str(td.get("step_id") or "").lower()
                if step_id and step_id in step_dirs:
                    step_dir = step_dirs[step_id]
                    if td.get("type") == "agent":
                        pre_jpg = step_dir / "pre.jpg"
                        if pre_jpg.exists():
                            td["payload"]["pre_screenshot_path"] = f"file://{pre_jpg.resolve()}"
                    elif td.get("type") == "action":
                        post_jpg = step_dir / "post.jpg"
                        if post_jpg.exists():
                            td["payload"]["post_screenshot_path"] = f"file://{post_jpg.resolve()}"

        return all_traces

    def load_steps_metadata(self, session_id: str) -> dict[int, dict]:
        """Loads metadata for all steps in the session from the chunked data."""
        session_chunked_dir = self.test_data_dir / f"{session_id}_chunked"

        steps = {}
        if session_chunked_dir.exists():
            for subdir in session_chunked_dir.iterdir():
                if subdir.is_dir() and subdir.name.startswith("step_"):
                    step_json_path = subdir / "step.json"
                    if step_json_path.exists():
                        try:
                            with open(step_json_path, encoding="utf-8") as f:
                                step_data = json.load(f)
                            step_num = step_data.get("step_number")
                            if step_num is not None:
                                steps[int(step_num)] = step_data
                        except Exception as e:
                            print(f"Warning: Failed to load step data from {step_json_path}: {e}")
        return steps

    def make_file_url(self, val):
        if isinstance(val, str) and (val.endswith(".jpg") or val.endswith(".png") or "/" in val):
            try:
                p = Path(val)
                if p.exists():
                    abs_path = str(p.resolve())
                    return (
                        f"file://{abs_path}" if abs_path.startswith("/") else f"file:///{abs_path}"
                    )
            except Exception:
                pass
        return val

    def clean_payload_paths(self, obj):
        if isinstance(obj, dict):
            return {k: self.clean_payload_paths(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self.clean_payload_paths(v) for v in obj]
        elif isinstance(obj, str):
            return self.make_file_url(obj)
        return obj

    def sanitize_nodes(self, nodes_list):
        sanitized = []
        for node in nodes_list:
            node["children"] = self.sanitize_nodes(node["children"])
            if node["name"] == "node":
                sanitized.extend(node["children"])
            else:
                sanitized.append(node)
        return sanitized

    def sort_tree_nodes_chronologically(self, nodes_list: list[dict]) -> list[dict]:
        for node in nodes_list:
            if "children" in node and node["children"]:
                node["children"] = self.sort_tree_nodes_chronologically(node["children"])
        return sorted(nodes_list, key=lambda x: x.get("timestamp", 0.0) or 0.0)

    def extract_tool_calls_from_db(self, temp_db_path: Path, preloaded_trace_ids: set) -> list:
        """Extracts tool calls executed during the current step replay from the sandbox database."""
        preloaded_set = {str(tid).lower() for tid in preloaded_trace_ids if tid}

        if not temp_db_path.exists():
            return []

        try:
            conn = sqlite3.connect(temp_db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT trace_id, name, status, payload FROM traces WHERE type"
                " = 'tool' ORDER BY timestamp ASC"
            )
            rows = cursor.fetchall()
            conn.close()
        except Exception as e:
            print(
                f"Warning: Failed to query tool calls from database: {e}",
                file=sys.stderr,
            )
            return []

        tool_calls = []
        for r in rows:
            tid = str(r["trace_id"]).lower()
            if tid not in preloaded_set:
                payload = {}
                if r["payload"]:
                    try:
                        payload = json.loads(r["payload"])
                    except Exception:
                        pass

                response_val = payload.get("result")
                if response_val is None:
                    response_val = payload.get("error")
                else:
                    if isinstance(response_val, str) and (
                        response_val.startswith("{") or response_val.startswith("[")
                    ):
                        try:
                            response_val = json.loads(response_val)
                        except Exception:
                            pass

                args = payload.get("args", {})
                positional_args = payload.get("positional_args", [])
                if positional_args:
                    if r["name"] == "search_by_coords" and len(positional_args) >= 2:
                        try:
                            args = {
                                "nx": int(positional_args[0]),
                                "ny": int(positional_args[1]),
                            }
                        except ValueError:
                            args = {
                                "nx": positional_args[0],
                                "ny": positional_args[1],
                            }
                    elif r["name"] == "search_ui" and len(positional_args) >= 1:
                        args = {"search_query": positional_args[0]}
                    elif r["name"] == "detect_objects" and len(positional_args) >= 1:
                        args = {"labels": positional_args[0]}
                    else:
                        args = {"positional_args": positional_args}

                tool_calls.append(
                    {
                        "name": r["name"],
                        "args": args,
                        "status": r["status"],
                        "response": response_val,
                    }
                )
        return tool_calls

    def extract_state_prepopulation_data(self, step_dir, step_data, pre_image_meta):
        """Dynamically extracts state prepopulation fields from chunked test step files."""
        latest_ui_hierarchy = None
        if pre_image_meta and "ui_tree" in pre_image_meta:
            latest_ui_hierarchy = pre_image_meta["ui_tree"]

        structured_decisions = None
        traces_json_path = step_dir / "traces.json"
        if traces_json_path.exists():
            try:
                with open(traces_json_path, encoding="utf-8") as f:
                    traces = json.load(f)
                operator_trace = next(
                    (t for t in traces if t.get("type") == "agent" and t.get("name") == "operator"),
                    None,
                )
                if operator_trace:
                    payload = operator_trace.get("payload") or {}
                    result_val = payload.get("result")
                    if isinstance(result_val, str):
                        try:
                            res_dict = json.loads(result_val)
                            structured_decisions = res_dict.get("structured_decisions")
                        except Exception as e:
                            print(f"Warning: Failed to parse operator trace result JSON: {e}")
                    elif isinstance(result_val, dict):
                        structured_decisions = result_val.get("structured_decisions")
            except Exception as e:
                print(f"Warning: Failed to read structured_decisions from traces.json: {e}")
                pass

        if not structured_decisions and step_data:
            action_taken = step_data.get("action_taken")
            if action_taken:
                try:
                    structured_decisions = json.dumps(action_taken)
                except Exception as e:
                    print(f"Warning: Failed to serialize action_taken fallback to JSON: {e}")
                    pass

        focused_app_info = None
        if latest_ui_hierarchy:
            system_packages = {
                "com.android.systemui",
                "android",
                "com.google.android.apps.nexuslauncher",
            }
            packages = []
            for node in latest_ui_hierarchy:
                if isinstance(node, dict):
                    pkg = node.get("package")
                    if pkg and pkg not in system_packages and "launcher" not in pkg.lower():
                        packages.append(pkg)
            if packages:
                from collections import Counter

                focused_app_info = Counter(packages).most_common(1)[0][0]

        device_date = None
        timestamp = None
        if step_data and "timestamp" in step_data:
            timestamp = step_data["timestamp"]
        elif pre_image_meta and "timestamp" in pre_image_meta:
            timestamp = pre_image_meta["timestamp"]

        if timestamp is not None:
            from datetime import datetime, UTC

            try:
                dt = datetime.fromtimestamp(timestamp, UTC)
                device_date = dt.strftime("%a %b %d %H:%M:%S UTC %Y")
            except Exception as e:
                print(
                    f"Warning: Failed to parse timestamp {timestamp} into device date string: {e}"
                )

        return (
            latest_ui_hierarchy,
            structured_decisions,
            focused_app_info,
            device_date,
        )

    def _ensure_session_chunked(self, session_id: str) -> Path:
        """Ensures that the session traces are chunked and ready for replay."""
        session_chunked_dir = self.test_data_dir / f"{session_id}_chunked"

        db_steps_count = 0
        if self.db_path.exists():
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COUNT(*) FROM steps WHERE session_id = ?",
                    (session_id,),
                )
                db_steps_count = cursor.fetchone()[0]
                conn.close()
            except Exception as e:
                print(f"Warning: Failed to query step count from DB: {e}")

        chunked_steps_count = 0
        if session_chunked_dir.exists():
            chunked_steps_count = len(list(session_chunked_dir.glob("step_*")))

        # Check if directory does not exist, does not contain the sentinel file, or count of chunked steps is out of sync
        needs_chunking = (
            not session_chunked_dir.exists()
            or not (session_chunked_dir / ".chunked").exists()
            or db_steps_count != chunked_steps_count
        )

        if needs_chunking:
            try:
                print(
                    f"Chunked data needs update for session {session_id} (DB"
                    f" steps: {db_steps_count}, chunked steps:"
                    f" {chunked_steps_count}). Chunking on-the-fly..."
                )
                self.chunk_session_traces(session_id, session_chunked_dir)
            except Exception as e:
                print(f"Failed to chunk session {session_id} on-the-fly: {e}")
        return session_chunked_dir

    def resolve_master_video_path(self, db_recorded_path_str: str) -> Path | None:
        """Resolves the physical master video recording path on disk, searching for renamed folders."""
        db_video_path = Path(db_recorded_path_str)

        # 1. Direct absolute path check
        if db_video_path.exists():
            return db_video_path

        # 2. Check if relative to workspace
        workspace_rel_path = self.workspace_root / db_video_path
        if workspace_rel_path.exists():
            return workspace_rel_path.resolve()

        # 3. Check if under traces_path
        traces_rel_path = self.traces_path / db_video_path.name
        if traces_rel_path.exists():
            return traces_rel_path.resolve()

        # 4. Resolve folder renaming prefix
        parent_dir_name = db_video_path.parent.name
        filename = db_video_path.name
        for search_dir in [
            self.traces_path,
            self.workspace_root / "artemis-traces",
        ]:
            if parent_dir_name and search_dir.exists():
                try:
                    for child in search_dir.iterdir():
                        if child.is_dir() and (
                            child.name.startswith(parent_dir_name) or parent_dir_name in child.name
                        ):
                            candidate = child / filename
                            if candidate.exists():
                                return candidate.resolve()
                except Exception as e:
                    print(f"Warning: Error while scanning {search_dir} for renamed folder: {e}")

        # 5. Check if under traces_path or artemis-traces with parent_dir_name
        for search_dir in [
            self.traces_path,
            self.workspace_root / "artemis-traces",
        ]:
            fallback_path = search_dir / parent_dir_name / filename
            if fallback_path.exists():
                return fallback_path.resolve()

        return None

    def calculate_virtual_start_time(self, session_id: str, step_number: int) -> float | None:
        """Calculates the virtual start time based on the database's final response timestamp."""
        if not self.db_path.exists():
            return None

        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            # Fetch fallback current step timestamp
            cursor.execute(
                "SELECT timestamp FROM steps WHERE session_id = ? AND step_number = ?",
                (session_id, step_number),
            )
            row = cursor.fetchone()
            step_timestamp = row[0] if (row and row[0] is not None) else None

            if step_number <= 1:
                # First step starts at session start
                cursor.execute(
                    "SELECT start_time FROM sessions WHERE session_id = ?",
                    (session_id,),
                )
                row = cursor.fetchone()
                db_session_start_time = row[0] if (row and row[0] is not None) else step_timestamp
                conn.close()
                return db_session_start_time

            # Get step_id of previous step
            cursor.execute(
                "SELECT step_id FROM steps WHERE session_id = ? AND step_number = ?",
                (session_id, step_number - 1),
            )
            row = cursor.fetchone()
            if not row:
                conn.close()
                return step_timestamp

            prev_step_id = row[0]

            # Get maximum trace timestamp for that step (final response timestamp)
            cursor.execute(
                "SELECT MAX(timestamp) FROM traces WHERE step_id = ?",
                (prev_step_id,),
            )
            row = cursor.fetchone()
            conn.close()

            if row and row[0] is not None:
                db_final_response_timestamp = row[0]
                return db_final_response_timestamp - 0.001

            return step_timestamp
        except Exception as e:
            print(f"Warning: Failed to calculate virtual start time from database: {e}")
            return None

    def _preemptive_clip_video(
        self,
        session_id: str,
        step_number: int,
        step_data: dict,
        temp_db_path: Path,
        outputs_dir: Path,
        replay_id: str = None,
    ):
        """Locates the master recording, calculates step time bounds, trims the video and updates the temp DB."""
        import subprocess
        import uuid

        # 1. Query the master database's video_recordings table
        if not self.db_path.exists():
            print(
                f"Warning: Master database not found at {self.db_path}."
                " Skipping preemptive video clip."
            )
            return

        try:
            conn_master = sqlite3.connect(self.db_path)
            cursor = conn_master.cursor()
            cursor.execute(
                "SELECT video_id, device_id, start_time, end_time,"
                " local_video_path FROM video_recordings WHERE session_id = ?",
                (session_id,),
            )
            row = cursor.fetchone()
            conn_master.close()
        except Exception as e:
            print(f"Warning: Failed to query video_recordings from master database: {e}")
            return

        if not row:
            print(
                f"No video recording found for session {session_id} in master"
                " database. Skipping clip."
            )
            return

        (
            video_id,
            device_id,
            video_start_time,
            video_end_time,
            db_recorded_path,
        ) = row
        if not db_recorded_path:
            print("Warning: Recorded path is empty in video_recordings. Skipping clip.")
            return

        # 2. Locate master recording on disk (renaming logic)
        master_video_path = self.resolve_master_video_path(db_recorded_path)
        if not master_video_path:
            print(
                f"Warning: Could not locate master recording on disk for path: {db_recorded_path}"
            )
            return

        # 3. Calculate step bounds using virtual start time
        step_start = self.calculate_virtual_start_time(session_id, step_number)
        if step_start is None:
            step_start = step_data.get("timestamp")

        if step_start is None:
            print("Warning: Step start timestamp could not be resolved. Skipping clip.")
            return

        # Look for next step start
        step_dir = self.test_data_dir / f"{session_id}_chunked" / f"step_{step_number:02d}"
        next_step_dir = step_dir.parent / f"step_{step_number + 1:02d}"
        next_step_json_path = next_step_dir / "step.json"

        if next_step_json_path.exists():
            try:
                with open(next_step_json_path, encoding="utf-8") as f:
                    next_step_data = json.load(f)
                next_step_start = next_step_data.get("timestamp", step_start + 15.0)
            except Exception as e:
                print(f"Warning: Failed to read next step json: {e}")
                next_step_start = step_start + 15.0
        else:
            next_step_start = step_start + 15.0

        # Calculate offsets
        start_offset = step_start - video_start_time
        # clamp start_offset to be non-negative
        start_offset = max(0.0, start_offset)
        end_offset = next_step_start - video_start_time
        duration = max(0.1, end_offset - start_offset)

        trimmed_video_path = outputs_dir / "recording_trimmed.mp4"

        # 4. Execute FFmpeg trim
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(master_video_path),
            "-ss",
            f"{start_offset:.3f}",
            "-t",
            f"{duration:.3f}",
            "-c:v",
            "libx264",
            str(trimmed_video_path),
        ]
        print(
            f"Trimming video segment from {master_video_path}:"
            f" offset={start_offset:.3f}s, duration={duration:.3f}s"
        )
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res.returncode != 0:
                print(
                    f"Warning: ffmpeg failed with exit code {res.returncode}. stderr: {res.stderr}"
                )
                return
        except Exception as e:
            print(f"Warning: Failed to execute ffmpeg command: {e}")
            return

        # 5. Update Sandbox Database
        try:
            conn_temp = sqlite3.connect(temp_db_path)
            conn_temp.execute("""
                CREATE TABLE IF NOT EXISTS video_recordings (
                    video_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    device_id TEXT,
                    start_time REAL,
                    end_time REAL,
                    local_video_path TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                )
            """)

            # Insert original metadata first if not present
            conn_temp.execute(
                """
                INSERT OR IGNORE INTO video_recordings (video_id, session_id, device_id, start_time, end_time, local_video_path)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (
                    video_id,
                    session_id,
                    device_id,
                    video_start_time,
                    video_end_time,
                    db_recorded_path,
                ),
            )

            # Update values
            conn_temp.execute(
                """
                UPDATE video_recordings 
                SET local_video_path = ?, start_time = ?, end_time = ? 
                WHERE session_id = ?
            """,
                (
                    str(trimmed_video_path),
                    step_start,
                    next_step_start,
                    session_id,
                ),
            )

            if replay_id:
                # Also copy / update for replay_id
                conn_temp.execute(
                    """
                    INSERT OR IGNORE INTO video_recordings (video_id, session_id, device_id, start_time, end_time, local_video_path)
                    VALUES (?, ?, ?, ?, ?, ?)
                """,
                    (
                        str(uuid.uuid4()),
                        replay_id,
                        device_id,
                        video_start_time,
                        video_end_time,
                        db_recorded_path,
                    ),
                )

                conn_temp.execute(
                    """
                    UPDATE video_recordings 
                    SET local_video_path = ?, start_time = ?, end_time = ? 
                    WHERE session_id = ?
                """,
                    (
                        str(trimmed_video_path),
                        step_start,
                        next_step_start,
                        replay_id,
                    ),
                )

            conn_temp.commit()
            conn_temp.close()
            print(
                f"Successfully updated sandbox database at {temp_db_path} with"
                " trimmed video segment."
            )
        except Exception as e:
            print(f"Warning: Failed to update sandbox SQLite database: {e}")

    def create_ctx(
        self,
        session_id: str,
        step_number: int,
        agent_name: str = "explorer",
        replay_id: str = None,
    ) -> ArtemisContext:
        """Creates a sandboxed ArtemisContext and DataEngine for the given session and step."""
        self._ensure_session_chunked(session_id)
        step_dir = self.test_data_dir / f"{session_id}_chunked" / f"step_{step_number:02d}"
        if not step_dir.exists():
            raise FileNotFoundError(f"Step directory not found at {step_dir}")

        step_json_path = step_dir / "step.json"
        pre_image_meta_path = step_dir / "pre_image_meta.json"
        post_image_meta_path = step_dir / "post_image_meta.json"

        with open(step_json_path, encoding="utf-8") as f:
            step_data = json.load(f)

        pre_image_meta = None
        if pre_image_meta_path.exists():
            with open(pre_image_meta_path, encoding="utf-8") as f:
                pre_image_meta = json.load(f)

        post_image_meta = None
        if post_image_meta_path.exists():
            with open(post_image_meta_path, encoding="utf-8") as f:
                post_image_meta = json.load(f)

        outputs_dir = self.test_outputs_dir / f"{session_id}_step_{step_number:02d}"
        outputs_dir.mkdir(parents=True, exist_ok=True)

        temp_traces_dir = outputs_dir / "temp_traces"
        temp_db_path = temp_traces_dir / "data_engine.db"

        _, _, traces_to_preload = self.extract_replay_data(
            step_dir, str(self.db_path), agent_name=agent_name
        )
        initial_goal = self.load_session_goal(session_id, step_dir, str(self.db_path))

        self.setup_temporary_database(
            temp_db_path,
            temp_traces_dir,
            session_id,
            step_data,
            pre_image_meta,
            initial_goal,
            traces_to_preload,
            post_image_meta=post_image_meta,
            replay_id=replay_id,
        )

        try:
            self._preemptive_clip_video(
                session_id=session_id,
                step_number=step_number,
                step_data=step_data,
                temp_db_path=temp_db_path,
                outputs_dir=outputs_dir,
                replay_id=replay_id,
            )
        except Exception as e:
            print(f"Warning: Pre-emptive video clipping failed: {e}")

        try:
            from artemis.context import (
                ArtemisContext,
                DeviceContext,
                DevicePlatform,
                ExecutionSetup,
            )
            from artemis.data_engine.engine import DataEngine

            ArtemisContext.model_rebuild()
        except ImportError as import_err:
            raise ImportError(f"Failed to import Artemis core modules: {import_err}")

        import sqlite3

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT device_info FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        row = cursor.fetchone()
        conn.close()

        device_info = {}
        if row and row[0]:
            try:
                device_info = json.loads(row[0])
            except Exception:
                pass

        sim_device_id = device_info.get("device_id", "replay-device")
        sim_w = device_info.get("device_width", 1080)
        sim_h = device_info.get("device_height", 2400)

        device_context = DeviceContext(
            host_platform="LINUX",
            mobile_platform=DevicePlatform.ANDROID,
            device_id=sim_device_id,
            device_width=sim_w,
            device_height=sim_h,
        )
        print(f"Replay simulated device: {sim_device_id} ({sim_w}x{sim_h})")

        from artemis.config import get_default_llm_config

        try:
            llm_cfg = get_default_llm_config()
        except Exception as e:
            print(f"Warning: Failed to load default LLM config: {e}")
            llm_cfg = None

        ctx = ArtemisContext(
            device=device_context,
            adb_client=None,
            ui_adb_client=None,
            execution_setup=ExecutionSetup(traces_path=temp_traces_dir),
            llm_config=llm_cfg,
        )

        data_engine = DataEngine(ctx)
        data_engine.current_session_id = replay_id or session_id
        data_engine.current_step_id = step_data["step_id"]
        data_engine.current_step_dir = outputs_dir
        ctx.data_engine = data_engine

        settings.DATA_ENGINE_DB_PATH = temp_db_path
        settings.TRACES_PATH = temp_traces_dir
        os.environ["DATA_ENGINE_DB_PATH"] = str(temp_db_path)

        return ctx

    def instantiate_state(
        self,
        session_id: str,
        step_number: int,
    ) -> State:
        """Instantiates the State object for the given session and step number."""
        self._ensure_session_chunked(session_id)
        step_dir = self.test_data_dir / f"{session_id}_chunked" / f"step_{step_number:02d}"
        if not step_dir.exists():
            raise FileNotFoundError(f"Step directory not found at {step_dir}")

        step_json_path = step_dir / "step.json"
        pre_image_meta_path = step_dir / "pre_image_meta.json"

        with open(step_json_path, encoding="utf-8") as f:
            step_data = json.load(f)

        pre_image_meta = None
        if pre_image_meta_path.exists():
            with open(pre_image_meta_path, encoding="utf-8") as f:
                pre_image_meta = json.load(f)

        initial_goal = self.load_session_goal(session_id, step_dir, str(self.db_path))

        ui_hier, decisions, app_info, dev_date = self.extract_state_prepopulation_data(
            step_dir, step_data, pre_image_meta
        )

        try:
            from artemis.graph.state import State
        except ImportError as import_err:
            raise ImportError(f"Failed to import State module: {import_err}")

        state = State(
            messages=[],
            initial_goal=initial_goal,
            latest_screenshot=str(step_dir / "pre.jpg")
            if (step_dir / "pre.jpg").exists()
            else str(step_dir / "post.jpg"),
            operator_raw_data={"width": self.w, "height": self.h},
            current_step_id=step_data["step_id"],
            complete_subgoals_by_ids=[],
            validator_messages=[],
            subagent_calls=step_data.get("subagent_calls", []),
            latest_ui_hierarchy=ui_hier,
            focused_app_info=app_info,
            device_date=dev_date,
            structured_decisions=decisions,
        )
        return state

    def generate_replay_id(self) -> str:
        """Generates a new unique replay ID."""
        import uuid

        return str(uuid.uuid4())

    async def replay_step_tool(
        self,
        session_id: str,
        step_number: int,
        override_device_id: str = None,
        user_submits: dict = None,
        tool_name: str = "ask_explorer",
        replay_id: str = None,
    ) -> list[dict]:
        """Replays a specific step tool in a sandboxed execution environment."""
        if not replay_id:
            replay_id = self.generate_replay_id()

        replay_result = {
            "session_id": session_id,
            "step_number": step_number,
            "replay_id": replay_id,
            "success": False,
        }
        tool_cfg = REPLAY_TOOLS_CONFIG.get(tool_name)
        if not tool_cfg:
            raise ValueError(f"Unknown tool for replay: {tool_name}")
        agent_name = tool_cfg["agent_name"]

        outputs_parent = self.test_outputs_dir
        if outputs_parent.exists():
            for item in outputs_parent.iterdir():
                if item.is_dir() and item.name.startswith(f"{session_id}_step_"):
                    archive_dir = self.replay_base_dir / "data" / "older"
                    archive_dir.mkdir(parents=True, exist_ok=True)

                    from datetime import datetime

                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    target_name = f"{item.name}_{timestamp}"
                    target_path = archive_dir / target_name

                    counter = 1
                    while target_path.exists():
                        target_name = f"{item.name}_{timestamp}_{counter}"
                        target_path = archive_dir / target_name
                        counter += 1

                    print(
                        f"Archiving previous run output directory {item.name} to {target_path}..."
                    )
                    try:
                        shutil.move(str(item), str(target_path))
                    except Exception as archive_err:
                        print(
                            f"Warning: Failed to archive {item.name}: {archive_err}",
                            file=sys.stderr,
                        )

        outputs_dir = self.test_outputs_dir / f"{session_id}_step_{step_number:02d}"
        temp_db_path = outputs_dir / "temp_traces" / "data_engine.db"

        # Load step data locally for fallback mapping resolutions
        step_dir = self.test_data_dir / f"{session_id}_chunked" / f"step_{step_number:02d}"
        step_json_path = step_dir / "step.json"
        with open(step_json_path, encoding="utf-8") as f:
            step_data = json.load(f)

        # Retrieve preloaded trace IDs from chunked trace files to filter new tool calls
        _, _, traces_to_preload = self.extract_replay_data(
            step_dir, str(self.db_path), agent_name=agent_name
        )
        preloaded_trace_ids = {t.get("trace_id") for t in traces_to_preload if t.get("trace_id")}
        llm_calls_log = []

        # Device initialization removed for step replay

        ctx = self.create_ctx(
            session_id=session_id,
            step_number=step_number,
            agent_name=agent_name,
            replay_id=replay_id,
        )

        state = self.instantiate_state(
            session_id=session_id,
            step_number=step_number,
        )

        data_engine = ctx.data_engine

        print("Executing using Live Gemini Client...")

        from google.genai.models import AsyncModels
        from unittest.mock import patch

        original_generate_content = AsyncModels.generate_content

        async def wrapped_generate_content(self, *, model, contents, config=None, **kwargs):
            system_instruction = None
            if config and config.system_instruction:
                if isinstance(config.system_instruction, str):
                    system_instruction = config.system_instruction
                elif hasattr(config.system_instruction, "parts"):
                    parts = []
                    for p in config.system_instruction.parts:
                        if hasattr(p, "text") and p.text:
                            parts.append(p.text)
                    system_instruction = " ".join(parts)
                else:
                    system_instruction = str(config.system_instruction)

            prompt_data = {
                "model": model,
                "system_instruction": system_instruction,
                "contents": [],
            }

            contents_list = contents if isinstance(contents, list) else [contents]
            for content in contents_list:
                parts_desc = []
                if hasattr(content, "parts") and content.parts:
                    for part in content.parts:
                        if hasattr(part, "text") and part.text:
                            parts_desc.append({"type": "text", "text": part.text})
                        elif hasattr(part, "inline_data") and part.inline_data:
                            parts_desc.append(
                                {
                                    "type": "image",
                                    "mime_type": part.inline_data.mime_type,
                                    "length": len(part.inline_data.data),
                                }
                            )
                        elif hasattr(part, "function_call") and part.function_call:
                            parts_desc.append(
                                {
                                    "type": "function_call",
                                    "name": part.function_call.name,
                                    "args": (
                                        dict(part.function_call.args)
                                        if part.function_call.args
                                        else {}
                                    ),
                                }
                            )
                        elif hasattr(part, "function_call") and part.function_response:
                            parts_desc.append(
                                {
                                    "type": "function_response",
                                    "name": part.function_response.name,
                                    "response": (
                                        dict(part.function_response.response)
                                        if part.function_response.response
                                        else {}
                                    ),
                                }
                            )
                else:
                    parts_desc.append({"type": "raw", "value": str(content)})

                prompt_data["contents"].append(
                    {
                        "role": getattr(content, "role", "unknown"),
                        "parts": parts_desc,
                    }
                )

            record = {"prompt": prompt_data, "response": None, "error": None}
            llm_calls_log.append(record)

            try:
                res = await original_generate_content(
                    self,
                    model=model,
                    contents=contents,
                    config=config,
                    **kwargs,
                )

                thinking_parts = []
                if hasattr(res, "candidates") and res.candidates and res.candidates[0].content:
                    for part in res.candidates[0].content.parts:
                        if getattr(part, "thought", False):
                            thinking_parts.append(part.text)

                resp_data = {
                    "text": res.text if hasattr(res, "text") else None,
                    "function_calls": [],
                    "thought": ("\n".join(thinking_parts) if thinking_parts else None),
                }
                if hasattr(res, "function_calls") and res.function_calls:
                    for fc in res.function_calls:
                        resp_data["function_calls"].append(
                            {
                                "name": fc.name,
                                "args": dict(fc.args) if fc.args else {},
                            }
                        )
                record["response"] = resp_data
                return res
            except Exception as err:
                err_str = str(err)
                record["error"] = (
                    f"{err.__class__.__name__}: {err_str}" if err_str else err.__class__.__name__
                )
                raise err

        try:
            try:
                import inspect
                import importlib

                mod = importlib.import_module(tool_cfg["module"])
                tool_fn = getattr(mod, tool_cfg["function"])

                user_submits = user_submits or {}
                sig = inspect.signature(tool_fn)
                tool_args = {}
                denylist = tool_cfg.get("denylist_args", [])
                fallback_mappings = tool_cfg.get("fallback_mappings", {})

                user_submits = user_submits or {}
                sig = inspect.signature(tool_fn)
                tool_args = {}
                for name, param in sig.parameters.items():
                    if name in denylist:
                        if name == "ctx":
                            tool_args["ctx"] = ctx
                        elif name == "state":
                            tool_args["state"] = state
                        continue

                    if name in user_submits:
                        val = user_submits[name]
                        if param.annotation is bool and isinstance(val, str):
                            val = val.lower() == "true"
                        tool_args[name] = val
                    else:
                        val = ""
                        if param.default is not inspect.Parameter.empty:
                            val = param.default
                        if name in fallback_mappings:
                            fallback_path = fallback_mappings[name]
                            resolved_val = self.resolve_step_data_path(step_data, fallback_path)
                            if resolved_val is not None:
                                val = resolved_val
                        tool_args[name] = val

                with patch.object(AsyncModels, "generate_content", wrapped_generate_content):
                    outcome = await tool_fn(**tool_args)

                replay_result["success"] = True
                replay_result["result"] = outcome
                print(f"Replay Execution complete. Outcome: {outcome}")

            except Exception as replay_err:
                print(f"Error during step replay: {replay_err}", file=sys.stderr)
                traceback.print_exc()

                replay_result["success"] = False
                replay_result["error"] = (
                    f"{replay_err.__class__.__name__}: {str(replay_err)}"
                    if str(replay_err)
                    else replay_err.__class__.__name__
                )
                raise replay_err
        finally:
            await data_engine.shutdown()

            tool_calls = []
            if temp_db_path.exists():
                try:
                    tool_calls = self.extract_tool_calls_from_db(temp_db_path, preloaded_trace_ids)
                except Exception as ex:
                    print(
                        f"Warning: Failed to extract tool calls: {ex}",
                        file=sys.stderr,
                    )

            replay_result["tool_calls"] = tool_calls
            replay_result["llm_calls"] = llm_calls_log

            outcome_path = outputs_dir / "result.json"
            try:
                with open(outcome_path, "w", encoding="utf-8") as f:
                    json.dump(replay_result, f, indent=2, ensure_ascii=False)
            except Exception as write_err:
                print(
                    f"Warning: Failed to write result.json: {write_err}",
                    file=sys.stderr,
                )

        return llm_calls_log

    def get_replay_config(self, tool_name: str = "ask_explorer") -> list[dict]:
        """Returns the parameter list and types of the selected tool logic."""
        tool_cfg = REPLAY_TOOLS_CONFIG.get(tool_name)
        if not tool_cfg:
            raise ValueError(f"Unknown tool for replay: {tool_name}")

        try:
            import inspect
            import importlib

            mod = importlib.import_module(tool_cfg["module"])
            tool_fn = getattr(mod, tool_cfg["function"])
            sig = inspect.signature(tool_fn)

            denylist = tool_cfg.get("denylist_args", [])
            explorer_params = []
            for name, param in sig.parameters.items():
                if name in denylist:
                    continue
                default_val = ""
                if param.default is not inspect.Parameter.empty:
                    default_val = param.default

                input_type = "text"
                if any(
                    keyword in name.lower()
                    for keyword in (
                        "feedback",
                        "context",
                        "description",
                        "note",
                    )
                ):
                    input_type = "textarea"
                elif param.annotation is bool:
                    input_type = "checkbox"
                elif param.annotation in (int, float):
                    input_type = "number"

                explorer_params.append(
                    {
                        "name": name,
                        "default": default_val,
                        "input_type": input_type,
                    }
                )
            return explorer_params
        except Exception as e:
            raise Exception(f"Failed to load signature for {tool_name}: {str(e)}")

    def get_replay_tools(self) -> list[dict]:
        """Returns the list of registered tools available for replay."""
        return [
            {
                "name": name,
                "display_name": cfg["display_name"],
                "description": cfg["description"],
            }
            for name, cfg in REPLAY_TOOLS_CONFIG.items()
        ]

    def get_replay_steps(self, session_id: str) -> list[dict]:
        """Loads and formats metadata for all chunked steps in the session."""
        session_chunked_dir = self._ensure_session_chunked(session_id)
        if not session_chunked_dir.exists():
            raise FileNotFoundError("Step replay chunked data not found for this session.")

        try:
            steps_metadata = self.load_steps_metadata(session_id)
            step_numbers = sorted(list(steps_metadata.keys()))

            import inspect
            import importlib

            # Pre-load signatures and configs for all registered tools
            tool_signatures = {}
            for t_name, t_cfg in REPLAY_TOOLS_CONFIG.items():
                try:
                    mod = importlib.import_module(t_cfg["module"])
                    fn = getattr(mod, t_cfg["function"])
                    tool_signatures[t_name] = inspect.signature(fn)
                except Exception as e:
                    print(
                        f"Warning: Failed to load signature for tool {t_name}: {e}",
                        file=sys.stderr,
                    )

            steps_list = []
            for num in step_numbers:
                s_data = steps_metadata.get(num, {})
                sid = s_data.get("step_id")

                pre_url = None
                post_url = None
                step_dir = session_chunked_dir / f"step_{num:02d}"
                if step_dir.exists():
                    pre_jpg = step_dir / "pre.jpg"
                    if pre_jpg.exists():
                        pre_url = f"file://{pre_jpg.resolve()}"
                    post_jpg = step_dir / "post.jpg"
                    if post_jpg.exists():
                        post_url = f"file://{post_jpg.resolve()}"
                    if pre_url and post_url and pre_url == post_url:
                        post_url = None

                # Generate param_defaults for all registered tools
                all_tool_defaults = {}
                for t_name, sig in tool_signatures.items():
                    t_cfg = REPLAY_TOOLS_CONFIG[t_name]
                    denylist = t_cfg.get("denylist_args", [])
                    fallback_mappings = t_cfg.get("fallback_mappings", {})

                    defaults = {}
                    for name, param in sig.parameters.items():
                        if name in denylist:
                            continue

                        val = ""
                        if param.default is not inspect.Parameter.empty:
                            val = param.default

                        # Resolve fallback using mappings
                        if name in fallback_mappings:
                            mapping_path = fallback_mappings[name]
                            parts = mapping_path.split(".")
                            curr = s_data
                            for part in parts:
                                if isinstance(curr, dict):
                                    curr = curr.get(part)
                                elif isinstance(curr, list):
                                    try:
                                        idx = int(part)
                                        if idx < len(curr):
                                            curr = curr[idx]
                                        else:
                                            curr = None
                                    except ValueError:
                                        curr = None
                                else:
                                    curr = None

                            if curr is not None:
                                val = curr

                        defaults[name] = val
                    all_tool_defaults[t_name] = defaults

                # Check if this step has already been replayed by scanning outputs folder
                step_outputs_dir = self.test_outputs_dir / f"{session_id}_step_{num:02d}"
                step_db_path = step_outputs_dir / "temp_traces" / "data_engine.db"
                is_replayed = step_db_path.exists()

                steps_list.append(
                    {
                        "step_id": str(sid).lower() if sid else None,
                        "step_number": num,
                        "timestamp": s_data.get("timestamp"),
                        "summary": s_data.get("summary", "No summary available."),
                        "action_taken": s_data.get("action_taken", []),
                        "query": "",
                        "param_defaults": all_tool_defaults,
                        "pre_screenshot": pre_url,
                        "post_screenshot": post_url,
                        "operator_raw_thinking": s_data.get("operator_raw_thinking"),
                        "operator_native_thinking": s_data.get("operator_native_thinking"),
                        "is_replayed": is_replayed,
                    }
                )

            return steps_list
        except Exception as e:
            traceback.print_exc()
            raise e

    async def run_step_replay(
        self,
        session_id: str,
        step_number: int,
        device_id: str,
        user_submits: dict,
        tool_name: str = "ask_explorer",
        replay_id: str = None,
    ) -> dict:
        """Triggers sandbox execution of step replay and returns the resulting traces trees."""
        tool_cfg = REPLAY_TOOLS_CONFIG.get(tool_name)
        if not tool_cfg:
            raise ValueError(f"Unknown tool for replay: {tool_name}")
        agent_name = tool_cfg["agent_name"]

        # 1. Run live step replay
        llm_calls_log = await self.replay_step_tool(
            session_id=session_id,
            step_number=step_number,
            override_device_id=device_id,
            user_submits=user_submits,
            tool_name=tool_name,
            replay_id=replay_id,
        )

        # 2. Extract freshly generated traces
        outputs_dir = self.test_outputs_dir / f"{session_id}_step_{step_number:02d}"
        temp_db_path = outputs_dir / "temp_traces" / "data_engine.db"

        self._ensure_session_chunked(session_id)
        step_dir = self.test_data_dir / f"{session_id}_chunked" / f"step_{step_number:02d}"
        _, _, traces_to_preload = self.extract_replay_data(
            step_dir, str(self.db_path), agent_name=agent_name
        )
        preloaded_trace_ids = {t.get("trace_id") for t in traces_to_preload if t.get("trace_id")}

        all_traces = self.load_all_session_traces(temp_db_path, preloaded_trace_ids, step_number)

        # Align LLM calls and extract final tool result from result.json if available
        replay_outcome = None
        replay_id = None
        result_json_path = outputs_dir / "result.json"
        if result_json_path.exists():
            try:
                with open(result_json_path, encoding="utf-8") as f:
                    res_data = json.load(f)
                    replay_outcome = res_data.get("result")
                    replay_id = res_data.get("replay_id")
            except Exception:
                pass

        # Update virtual tool trace result with the actual formatted outcome if available
        if replay_outcome is not None:
            for node in all_traces:
                if (
                    not node.get("is_preloaded")
                    and node["type"] == "tool"
                    and node["name"] == tool_name
                ):
                    node["payload"]["result"] = replay_outcome

        # Collect all valid LLM span names from REPLAY_TOOLS_CONFIG to align subagent calls too
        all_llm_span_names = {
            cfg.get("llm_span_name")
            for cfg in REPLAY_TOOLS_CONFIG.values()
            if cfg.get("llm_span_name")
        }

        llm_call_index = 0
        for node in all_traces:
            if (
                not node.get("is_preloaded")
                and node["type"] == "span"
                and node["name"] in all_llm_span_names
                and llm_calls_log
                and llm_call_index < len(llm_calls_log)
            ):
                node["type"] = "llm_call"
                node["llm_call"] = llm_calls_log[llm_call_index]
                llm_call_index += 1

        # Clean payload paths
        for node in all_traces:
            if node.get("payload"):
                node["payload"] = self.clean_payload_paths(node["payload"])
            if node.get("llm_call"):
                node["llm_call"] = self.clean_payload_paths(node["llm_call"])

        # Filter all_traces for ONLY this step's traces
        step_traces = [t for t in all_traces if t.get("step_number") == step_number]

        preloaded_traces = [t for t in step_traces if t.get("is_preloaded")]
        live_traces = [t for t in step_traces if not t.get("is_preloaded")]

        # Build preloaded hierarchy
        preloaded_nodes = {
            str(t["trace_id"]).lower(): {**t, "children": []} for t in preloaded_traces
        }
        preloaded_root_nodes = []
        for node_id, node in preloaded_nodes.items():
            parent_id = node.get("parent_trace_id")
            if parent_id:
                parent_id_str = str(parent_id).lower()
                if parent_id_str in preloaded_nodes:
                    preloaded_nodes[parent_id_str]["children"].append(node)
                else:
                    preloaded_root_nodes.append(node)
            else:
                preloaded_root_nodes.append(node)
        preloaded_root_nodes = self.sanitize_nodes(preloaded_root_nodes)
        preloaded_root_nodes = self.sort_tree_nodes_chronologically(preloaded_root_nodes)

        # Build live hierarchy
        live_nodes = {str(t["trace_id"]).lower(): {**t, "children": []} for t in live_traces}

        # Add a virtual "Response" child node at the end of the virtual tool trace to show the final outcome at the bottom of the tree
        if replay_outcome is not None:
            virtual_tool_node_id = None
            for node_id, node in live_nodes.items():
                if node.get("type") == "tool" and node.get("name") == tool_name:
                    virtual_tool_node_id = node_id
                    break

            if virtual_tool_node_id:
                import uuid

                outcome_node_id = str(uuid.uuid4())
                max_ts = (
                    max([t.get("timestamp", 0.0) for t in live_traces])
                    if live_traces
                    else live_nodes[virtual_tool_node_id].get("timestamp", 0.0)
                )

                outcome_node = {
                    "trace_id": outcome_node_id,
                    "session_id": session_id,
                    "step_id": live_nodes[virtual_tool_node_id].get("step_id"),
                    "parent_trace_id": live_nodes[virtual_tool_node_id]["trace_id"],
                    "type": "tool",
                    "name": f"{tool_name} Response",
                    "status": (live_nodes[virtual_tool_node_id].get("status", "success")),
                    "timestamp": max_ts + 0.001,
                    "duration": 0.0,
                    "payload": {"result": replay_outcome},
                    "is_preloaded": False,
                    "step_number": step_number,
                    "children": [],
                }
                live_nodes[outcome_node_id] = outcome_node

        live_root_nodes = []
        for node_id, node in live_nodes.items():
            parent_id = node.get("parent_trace_id")
            if parent_id:
                parent_id_str = str(parent_id).lower()
                if parent_id_str in live_nodes:
                    live_nodes[parent_id_str]["children"].append(node)
                else:
                    live_root_nodes.append(node)
            else:
                live_root_nodes.append(node)
        live_root_nodes = self.sanitize_nodes(live_root_nodes)
        live_root_nodes = self.sort_tree_nodes_chronologically(live_root_nodes)

        return {
            "success": True,
            "preloaded": preloaded_root_nodes,
            "live": live_root_nodes,
            "replay_id": replay_id,
        }

    def get_step_replay_traces(
        self, session_id: str, step_number: int, tool_name: str = "ask_explorer"
    ) -> dict:
        """Retrieves previously generated step replay traces if they exist."""
        tool_cfg = REPLAY_TOOLS_CONFIG.get(tool_name)
        if not tool_cfg:
            raise ValueError(f"Unknown tool for replay: {tool_name}")
        agent_name = tool_cfg["agent_name"]

        outputs_dir = self.test_outputs_dir / f"{session_id}_step_{step_number:02d}"
        temp_db_path = outputs_dir / "temp_traces" / "data_engine.db"

        if not temp_db_path.exists():
            return {"success": True, "live": []}

        self._ensure_session_chunked(session_id)
        step_dir = self.test_data_dir / f"{session_id}_chunked" / f"step_{step_number:02d}"
        preloaded_trace_ids = set()
        if step_dir.exists():
            _, _, traces_to_preload = self.extract_replay_data(
                step_dir,
                str(self.db_path),
                agent_name=agent_name,
                tool_name=tool_name,
            )
            preloaded_trace_ids = {
                t.get("trace_id") for t in traces_to_preload if t.get("trace_id")
            }

        all_traces = self.load_all_session_traces(temp_db_path, preloaded_trace_ids, step_number)

        llm_span_name = tool_cfg.get("llm_span_name")
        if not llm_span_name:
            raise ValueError(f"Tool {tool_name} config is missing 'llm_span_name'")

        # Align LLM calls and extract final tool result from result.json if available
        llm_calls_log = []
        replay_outcome = None
        replay_id = None
        result_json_path = outputs_dir / "result.json"
        if result_json_path.exists():
            try:
                with open(result_json_path, encoding="utf-8") as f:
                    res_data = json.load(f)
                    llm_calls_log = res_data.get("llm_calls") or []
                    replay_outcome = res_data.get("result")
                    replay_id = res_data.get("replay_id")
            except Exception:
                pass

        # Update virtual tool trace result with the actual formatted outcome if available
        if replay_outcome is not None:
            for node in all_traces:
                if (
                    not node.get("is_preloaded")
                    and node["type"] == "tool"
                    and node["name"] == tool_name
                ):
                    node["payload"]["result"] = replay_outcome

        # Collect all valid LLM span names from REPLAY_TOOLS_CONFIG to align subagent calls too
        all_llm_span_names = {
            cfg.get("llm_span_name")
            for cfg in REPLAY_TOOLS_CONFIG.values()
            if cfg.get("llm_span_name")
        }

        llm_call_index = 0
        for node in all_traces:
            if (
                not node.get("is_preloaded")
                and node["type"] == "span"
                and node["name"] in all_llm_span_names
                and llm_calls_log
                and llm_call_index < len(llm_calls_log)
            ):
                node["type"] = "llm_call"
                node["llm_call"] = llm_calls_log[llm_call_index]
                llm_call_index += 1

        # Clean payload paths
        for node in all_traces:
            if node.get("payload"):
                node["payload"] = self.clean_payload_paths(node["payload"])
            if node.get("llm_call"):
                node["llm_call"] = self.clean_payload_paths(node["llm_call"])

        # Filter all_traces for ONLY this step's traces
        step_traces = [t for t in all_traces if t.get("step_number") == step_number]
        live_traces = [t for t in step_traces if not t.get("is_preloaded")]

        # Build live hierarchy
        live_nodes = {str(t["trace_id"]).lower(): {**t, "children": []} for t in live_traces}

        # Add a virtual "Response" child node at the end of the virtual tool trace to show the final outcome at the bottom of the tree
        if replay_outcome is not None:
            virtual_tool_node_id = None
            for node_id, node in live_nodes.items():
                if node.get("type") == "tool" and node.get("name") == tool_name:
                    virtual_tool_node_id = node_id
                    break

            if virtual_tool_node_id:
                import uuid

                outcome_node_id = str(uuid.uuid4())
                max_ts = (
                    max([t.get("timestamp", 0.0) for t in live_traces])
                    if live_traces
                    else live_nodes[virtual_tool_node_id].get("timestamp", 0.0)
                )

                outcome_node = {
                    "trace_id": outcome_node_id,
                    "session_id": session_id,
                    "step_id": live_nodes[virtual_tool_node_id].get("step_id"),
                    "parent_trace_id": live_nodes[virtual_tool_node_id]["trace_id"],
                    "type": "tool",
                    "name": f"{tool_name} Response",
                    "status": (live_nodes[virtual_tool_node_id].get("status", "success")),
                    "timestamp": max_ts + 0.001,
                    "duration": 0.0,
                    "payload": {"result": replay_outcome},
                    "is_preloaded": False,
                    "step_number": step_number,
                    "children": [],
                }
                live_nodes[outcome_node_id] = outcome_node

        live_root_nodes = []
        for node_id, node in live_nodes.items():
            parent_id = node.get("parent_trace_id")
            if parent_id:
                parent_id_str = str(parent_id).lower()
                if parent_id_str in live_nodes:
                    live_nodes[parent_id_str]["children"].append(node)
                else:
                    live_root_nodes.append(node)
            else:
                live_root_nodes.append(node)
        live_root_nodes = self.sanitize_nodes(live_root_nodes)
        live_root_nodes = self.sort_tree_nodes_chronologically(live_root_nodes)

        return {
            "success": True,
            "live": live_root_nodes,
            "replay_id": replay_id,
        }
