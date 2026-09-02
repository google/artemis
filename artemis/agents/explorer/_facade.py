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

"""Late-bound access to the ``artemis.agents.explorer.explorer`` facade module.

The Explorer implementation is split across several modules, but external
callers (and ``unittest.mock.patch`` targets in the test suite) address
collaborators such as ``settings``, ``StorageManager``, ``draw_dots``,
``search_ui_func``, ``get_llm`` etc. through the historical module path
``artemis.agents.explorer.explorer``.  Split-out code therefore resolves those
names at call time via :func:`facade` instead of importing them directly, so
patches applied to the facade module keep affecting the split-out code.

The import happens inside the function (not at module top level) to stay safe
regardless of which module of the package is imported first.
"""


def facade():
    """Return the ``artemis.agents.explorer.explorer`` module object."""
    import artemis.agents.explorer.explorer as explorer_module

    return explorer_module
