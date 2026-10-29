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

"""Old import paths of the modules moved to ``third_party/mobile_use``.

An old path resolves to the new module object itself, so isinstance checks,
module-level state and ``patch("old.path.x")`` still work. The first import of
an old path emits a ``DeprecationWarning``.

This is a meta path finder rather than one stub file per path because
``artemis.agents.hopper`` is a package; a stub file would load
``artemis.agents.hopper.hopper`` a second time.
"""

import importlib
import importlib.abc
import importlib.machinery
import sys
from types import ModuleType
import warnings

_NEW_ROOT = "third_party.mobile_use"

# Old module path -> new module path.
LEGACY_MODULES: dict[str, str] = {
    "artemis.agents.hopper": f"{_NEW_ROOT}.agents.hopper",
    "artemis.agents.hopper.hopper": f"{_NEW_ROOT}.agents.hopper.hopper",
    "artemis.clients.ui_automator_client": f"{_NEW_ROOT}.clients.ui_automator_client",
    "artemis.config.output": f"{_NEW_ROOT}.config.output",
    "artemis.controllers.platform_specific_commands_controller": (
        f"{_NEW_ROOT}.controllers.platform_specific_commands_controller"
    ),
    "artemis.sdk.builders.index": f"{_NEW_ROOT}.sdk.builders.index",
    "artemis.sdk.builders.task_request_builder": f"{_NEW_ROOT}.sdk.builders.task_request_builder",
    "artemis.sdk.types.exceptions": f"{_NEW_ROOT}.sdk.types.exceptions",
    "artemis.sdk.types.task": f"{_NEW_ROOT}.sdk.types.task",
    "artemis.utils.cli_helpers": f"{_NEW_ROOT}.utils.cli_helpers",
    "artemis.utils.decorators": f"{_NEW_ROOT}.utils.decorators",
    "artemis.utils.file": f"{_NEW_ROOT}.utils.file",
    "artemis.utils.logger": f"{_NEW_ROOT}.utils.logger",
    "artemis.utils.media": f"{_NEW_ROOT}.utils.media",
    "artemis.utils.shell_utils": f"{_NEW_ROOT}.utils.shell_utils",
}


class _AliasLoader(importlib.abc.Loader):
    """Loader that hands back an already-importable module under another name."""

    def __init__(self, target: str) -> None:
        self._target = target
        self._target_spec: importlib.machinery.ModuleSpec | None = None

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType:
        warnings.warn(
            f"'{spec.name}' is deprecated; import '{self._target}' instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        module = importlib.import_module(self._target)
        self._target_spec = module.__spec__
        return module

    def exec_module(self, module: ModuleType) -> None:
        # Nothing to execute. The import system has overwritten __spec__ with the
        # old name; put the real spec back so reload() and pickling still work.
        if self._target_spec is not None:
            module.__spec__ = self._target_spec


class _LegacyModuleFinder(importlib.abc.MetaPathFinder):
    """Resolve entries of :data:`LEGACY_MODULES` to their new modules."""

    def find_spec(self, fullname, path=None, target=None):
        new_name = LEGACY_MODULES.get(fullname)
        if new_name is None:
            return None
        return importlib.machinery.ModuleSpec(fullname, _AliasLoader(new_name))


def install() -> None:
    """Register the finder once, ahead of the default path-based finders."""
    if not any(isinstance(finder, _LegacyModuleFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _LegacyModuleFinder())
