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

"""Access to immutable defaults and web assets shipped with Artemis.

Python wheels are installed as ordinary directories by pip, so callers that
need to hand a path to another library (for example FastAPI's ``FileResponse``)
can safely use the paths returned here. Source checkouts continue to prefer
their editable ``config`` and ``apps/showcase_ui/dist`` trees; these resources
are the installation fallback.
"""

from importlib.resources import files
from pathlib import Path


def _resource_path(*parts: str) -> Path:
    """Return a filesystem path inside the installed resource package."""
    resource = files(__name__).joinpath(*parts)
    return Path(str(resource))


def get_bundled_config_path(filename: str) -> Path | None:
    """Return a bundled configuration template when it exists."""
    candidate = _resource_path("config", filename)
    return candidate if candidate.is_file() else None


def get_bundled_showcase_dist() -> Path | None:
    """Return the bundled Angular browser build when it is complete."""
    candidate = _resource_path("showcase_ui")
    return candidate if (candidate / "index.html").is_file() else None


__all__ = ["get_bundled_config_path", "get_bundled_showcase_dist"]
