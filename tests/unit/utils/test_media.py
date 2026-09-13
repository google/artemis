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

from artemis.utils.media import (
    create_steps_json_from_trace_folder,
    remove_steps_json_from_trace_folder,
)


def test_step_compilation_preserves_recording_manifest(tmp_path):
    step_file = tmp_path / "123.json"
    step_file.write_text('{"action": "click"}', encoding="utf-8")
    recording_manifest = tmp_path / "recording.json"
    recording_manifest.write_text(
        '{"version": 1, "segments": [{"file": "recording.mp4"}]}',
        encoding="utf-8",
    )

    create_steps_json_from_trace_folder(tmp_path)

    compiled = json.loads((tmp_path / "steps.json").read_text(encoding="utf-8"))
    assert compiled == [{"timestamp": 123, "data": '{"action": "click"}'}]
    assert recording_manifest.exists()

    remove_steps_json_from_trace_folder(tmp_path)

    assert not step_file.exists()
    assert recording_manifest.exists()
    assert (tmp_path / "steps.json").exists()
