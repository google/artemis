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

import os
from artemis.utils.notes import update_note_content

base_dir = "/tmp/artemis_test"
os.makedirs(f"{base_dir}/notes", exist_ok=True)
task_plan = """- [x] Initial task
- [/] Active task
- [ ] Future task"""
with open(f"{base_dir}/notes/task_plan.md", "w") as f:
    f.write(task_plan)

try:
    res = update_note_content(
        base_dir,
        "task_plan",
        "- [/] Active task",
        "- [/] Active task\n    - [ ] New subgoal",
    )
    print("Update successful!")
    print(open(f"{base_dir}/notes/task_plan.md").read())
except Exception as e:
    print("Error:", e)
