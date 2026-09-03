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
from pathlib import Path
import shutil

from setuptools import Extension, find_namespace_packages, setup
from setuptools.command.build_py import build_py as _build_py
from setuptools.command.sdist import sdist as _sdist

USE_CYTHON = os.environ.get("USE_CYTHON", "0") == "1"


def _showcase_dist(source_root: Path) -> Path | None:
    """Locate a complete Angular browser build in a source or sdist tree."""
    base_dist = source_root / "apps" / "showcase_ui" / "dist"
    candidates = (
        base_dist / "frontend" / "browser",
        base_dist / "browser",
        base_dist / "frontend",
        base_dist,
        source_root / "artemis" / "resources" / "showcase_ui",
    )
    return next((path for path in candidates if (path / "index.html").is_file()), None)


def _copy_release_resources(source_root: Path, resource_root: Path) -> None:
    """Stage immutable runtime resources into a wheel or source distribution."""
    config_candidates = (
        source_root / "config" / "artemis.jsonc",
        source_root / "artemis" / "resources" / "config" / "artemis.jsonc",
    )
    config_source = next((path for path in config_candidates if path.is_file()), None)
    if config_source is None:
        raise RuntimeError("Cannot build Artemis: config/artemis.jsonc is missing.")

    showcase_source = _showcase_dist(source_root)
    if showcase_source is None:
        raise RuntimeError(
            "Cannot build an Artemis distribution without the Showcase UI. "
            "Run `npm ci --prefix apps/showcase_ui` and "
            "`npm run build --prefix apps/showcase_ui` first."
        )
    config_target = resource_root / "config"
    config_target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(config_source, config_target / "artemis.jsonc")

    showcase_target = resource_root / "showcase_ui"
    if showcase_target.exists():
        shutil.rmtree(showcase_target)
    shutil.copytree(showcase_source, showcase_target)


class build_py(_build_py):
    """Build Python modules, then stage config and precompiled web assets."""

    def run(self) -> None:
        super().run()
        # Editable installs use the checkout's config and frontend assets.
        if self.editable_mode:
            return
        # Older builds may have copied frontend sources and node_modules here.
        excluded_showcase = Path(self.build_lib) / "apps" / "showcase_ui"
        if excluded_showcase.exists():
            shutil.rmtree(excluded_showcase)
        _copy_release_resources(
            Path.cwd(),
            Path(self.build_lib) / "artemis" / "resources",
        )


class sdist(_sdist):
    """Make sdists self-contained so wheels can be built from their archive."""

    def make_release_tree(self, base_dir: str, files: list[str]) -> None:
        # Old egg-info manifests may include Python packages from node_modules.
        filtered_files = [
            path for path in files if not Path(path).as_posix().startswith("apps/showcase_ui/")
        ]
        super().make_release_tree(base_dir, filtered_files)
        # Protect local rebuilds from stale egg-info/SOURCES.txt manifests
        # generated before package discovery was narrowed.
        excluded_showcase = Path(base_dir) / "apps" / "showcase_ui"
        if excluded_showcase.exists():
            shutil.rmtree(excluded_showcase)
        _copy_release_resources(
            Path.cwd(),
            Path(base_dir) / "artemis" / "resources",
        )


ext_modules = []

if USE_CYTHON:
    from Cython.Build import cythonize

    print(f"USE_CYTHON: {USE_CYTHON}")

    extensions = []
    src_dir = Path("artemis")

    # Recursively find all .py files inside the artemis/ directory for optional acceleration
    for py_file in src_dir.rglob("*.py"):
        rel_path = py_file.relative_to(src_dir)
        mod_parts = ("artemis",) + rel_path.with_suffix("").parts
        module_name = ".".join(mod_parts)

        if module_name in (
            "artemis.__init__",
            "artemis.main",
            "artemis.mcp.adb_server",
            "artemis.mcp.xml_search_server",
        ):
            continue

        extensions.append(Extension(module_name, [str(py_file)]))

    ext_modules = cythonize(
        extensions,
        compiler_directives={"language_level": "3"},
        build_dir="build/cythonized",
    )

setup(
    # Name and version come from pyproject.toml.
    packages=find_namespace_packages(
        include=[
            "artemis",
            "artemis.*",
            "mcp_server",
            "mcp_server.*",
            "apps",
            "apps.admin_console",
            "apps.admin_console.*",
        ]
    ),
    package_data={
        "artemis": ["**/*.json", "**/*.md"],
        "artemis.resources": ["config/*.jsonc", "showcase_ui/*", "showcase_ui/**/*"],
        "apps.admin_console": ["index.html"],
    },
    include_package_data=False,
    ext_modules=ext_modules,
    exclude_package_data={"": ["*.c", "*.pyx", "*.pxd", "*.cpp"]},
    cmdclass={"build_py": build_py, "sdist": sdist},
)
