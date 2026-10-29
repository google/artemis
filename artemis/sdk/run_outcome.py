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

"""Run outcome helpers: test summary attachment and trace naming."""


def attach_test_summary(output, run_outcome: dict | None):
    """Surfaces the machine-readable test summary in the task's return value.

    Only applies when the run actually had check items (the summary carries at
    least one counted item) — otherwise the historical return shape is kept
    untouched. String outputs are wrapped into a dict so that callers such as
    ``mobile_run_task`` receive the summary without parsing report prose.
    """
    if not run_outcome:
        return output
    tests = run_outcome.get("tests") or {}
    total = (
        int(tests.get("passed", 0))
        + int(tests.get("failed", 0))
        + int(tests.get("inconclusive", 0))
        + int(tests.get("unchecked", 0))
    )
    if total <= 0:
        return output
    summary = {"task_status": run_outcome.get("task_status"), **tests}
    if isinstance(output, dict):
        merged = dict(output)
        merged.setdefault("test_summary", summary)
        return merged
    if output is None:
        return {"test_summary": summary}
    if isinstance(output, str):
        return {"result": output, "test_summary": summary}
    # Typed/structured outputs keep their shape; the summary stays available in
    # run_outcome.json and the session state.
    return output


def resolve_trace_suffix(task_status: str, run_outcome: dict | None) -> str:
    """Trace naming: assertion failures must be distinguishable from _PASS."""
    if task_status != "completed":
        return "_FAIL"
    tests = (run_outcome or {}).get("tests") or {}
    if int(tests.get("failed", 0)) > 0:
        return "_TESTFAIL"
    return "_PASS"
