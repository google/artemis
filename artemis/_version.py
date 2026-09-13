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

"""Runtime package version sourced exclusively from distribution metadata."""

from importlib.metadata import PackageNotFoundError, version


def get_version() -> str:
    """Return the installed Artemis version, or an honest source-tree sentinel."""
    try:
        return version("artemis")
    except PackageNotFoundError:
        return "0+unknown"


__version__ = get_version()

__all__ = ["__version__", "get_version"]
