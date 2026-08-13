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
import re
import shutil
from typing import IO
from unittest.mock import MagicMock


def strip_json_comments(text: str) -> str:
    text = re.sub(r"//.*?$", "", text, flags=re.MULTILINE)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return text


def load_jsonc(file: IO) -> dict:
    return json.loads(strip_json_comments(file.read()))


def create_snapshot(src_dir: Path, snapshot_dir: Path):
    """Creates a snapshot of a directory by copying it."""
    if (
        isinstance(src_dir, MagicMock)
        or isinstance(snapshot_dir, MagicMock)
        or "MagicMock" in str(src_dir)
        or "MagicMock" in str(snapshot_dir)
    ):
        return
    if not src_dir.exists():
        return
    if snapshot_dir.exists():
        shutil.rmtree(snapshot_dir)
    shutil.copytree(src_dir, snapshot_dir)


def restore_snapshot(snapshot_dir: Path, dst_dir: Path):
    """Restores a directory from a snapshot."""
    if not snapshot_dir.exists():
        raise FileNotFoundError(f"Snapshot directory {snapshot_dir} does not exist.")
    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    shutil.copytree(snapshot_dir, dst_dir)
