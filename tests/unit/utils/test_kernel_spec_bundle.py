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

"""The compiled bundle must be able to host its own Jupyter kernel."""

import json
import os
from pathlib import Path

from artemis.utils import python_executor


def test_source_checkout_uses_the_system_kernel(monkeypatch):
    monkeypatch.setattr(python_executor, "is_frozen_bundle", lambda: False)

    assert python_executor.resolve_kernel_name() == python_executor.SOURCE_KERNEL_NAME


def test_frozen_bundle_publishes_a_kernel_that_reenters_the_binary(monkeypatch, tmp_path):
    binary = tmp_path / "dist" / "artemis"
    monkeypatch.setattr(python_executor, "is_frozen_bundle", lambda: True)
    monkeypatch.setattr(python_executor, "get_app_dir", lambda: tmp_path)
    monkeypatch.setattr(python_executor.sys, "executable", str(binary))
    monkeypatch.delenv("JUPYTER_PATH", raising=False)

    name = python_executor.resolve_kernel_name()
    try:
        spec_path = tmp_path / "kernels" / name / "kernel.json"
        spec = json.loads(spec_path.read_text(encoding="utf-8"))

        assert name == python_executor.BUNDLED_KERNEL_NAME
        assert spec["argv"] == [str(binary), "--kernel", "-f", "{connection_file}"]
        assert os.environ["JUPYTER_PATH"] == str(tmp_path / "kernels")
    finally:
        os.environ.pop("JUPYTER_PATH", None)


def test_kernel_spec_is_rewritten_on_every_start(monkeypatch, tmp_path):
    monkeypatch.setattr(python_executor, "is_frozen_bundle", lambda: True)
    monkeypatch.setattr(python_executor, "get_app_dir", lambda: tmp_path)
    monkeypatch.setattr(python_executor.sys, "executable", "/opt/artemis")
    spec_path = Path(tmp_path / "kernels" / python_executor.BUNDLED_KERNEL_NAME / "kernel.json")
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text("stale", encoding="utf-8")

    python_executor.resolve_kernel_name()
    os.environ.pop("JUPYTER_PATH", None)

    assert json.loads(spec_path.read_text(encoding="utf-8"))["argv"][0] == "/opt/artemis"


def test_nuitka_binary_counts_as_frozen(monkeypatch):
    from artemis.config import paths

    monkeypatch.delattr(paths.sys, "frozen", raising=False)
    monkeypatch.setattr(paths, "__compiled__", object(), raising=False)

    assert paths.is_frozen_bundle() is True
