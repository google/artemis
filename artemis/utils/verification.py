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
from pathlib import Path
from artemis.utils.logger import get_logger
from artemis.utils.notes import get_notes_dir

logger = get_logger(__name__)


def get_verification_chat_path(base_dir: str | Path, subgoal_hash: str) -> Path:
    """Gets the path to the verification chat file for a given subgoal."""
    notes_dir = get_notes_dir(base_dir)
    return notes_dir / f"verification_chat_{subgoal_hash}.json"


def read_verification_chat(chat_path: Path) -> list[dict]:
    """Reads the verification chat history from the given path."""
    if not chat_path.exists():
        return []
    try:
        return json.loads(chat_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Failed to load verification chat from {chat_path}: {e}")
        return []


def get_verification_chat_rounds(turns: list[dict]) -> tuple[int, int]:
    """Calculates the maximum rounds for operator and checker from the turns."""
    max_op = max(
        [t.get("round", 0) for t in turns if t.get("role") == "operator"],
        default=0,
    )
    max_chk = max(
        [t.get("round", 0) for t in turns if t.get("role") == "checker"],
        default=0,
    )
    return max_op, max_chk


def append_verification_chat(chat_path: Path, role: str, content: str, round_num: int) -> bool:
    """Appends a new turn to the verification chat file."""
    turns = read_verification_chat(chat_path)
    turns.append({"role": role, "round": round_num, "content": content})
    try:
        chat_path.write_text(json.dumps(turns, indent=2, ensure_ascii=False), encoding="utf-8")
        return True
    except Exception as e:
        logger.error(f"Failed to save verification chat to {chat_path}: {e}")
        return False
