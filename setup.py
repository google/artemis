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
from setuptools import Extension, find_namespace_packages, setup
from setuptools.command.build_py import build_py

USE_CYTHON = os.environ.get("USE_CYTHON", "0") == "1"

ext_modules = []
cmdclass = {}

if USE_CYTHON:
    from Cython.Build import cythonize

    print(f"USE_CYTHON: {USE_CYTHON}")

    extensions = []
    src_dir = Path("artemis")

    # 1. Recursively find all .py files inside the src/ directory
    for py_file in src_dir.rglob("*.py"):
        # Get the path relative to src/ (e.g., mypackage/subfolder/module.py)
        rel_path = py_file.relative_to(src_dir)

        # Convert path into a Python module name (e.g., artemis.subfolder.module)
        mod_parts = ("artemis",) + rel_path.with_suffix("").parts
        module_name = ".".join(mod_parts)

        # PREVENT CYTHON CRASH: Skip rogue __init__.py files at the root of src/
        if module_name in (
            "artemis.__init__",
            "artemis.main",
            "artemis.mcp.adb_server",
            "artemis.mcp.xml_search_server",
        ):
            print(f"Skipping init/main file from Cython: {py_file}")
            continue

        # 2. Create an Extension for this specific file
        extensions.append(Extension(module_name, [str(py_file)]))

    # Compile all found extensions
    ext_modules = cythonize(
        extensions,
        compiler_directives={"language_level": "3"},
        build_dir="build/cythonized",
    )

    # 3. Prevent packaging original .py source files (except main and __init__) and intermediate .c files
    class BuildPyExcludeSource(build_py):
        def find_package_modules(self, package, package_dir):
            modules = super().find_package_modules(package, package_dir)
            return [
                m
                for m in modules
                if m[1] in ("main", "__init__", "adb_server", "xml_search_server")
            ]

        def find_data_files(self, package, package_dir):
            data_files = super().find_data_files(package, package_dir)
            return [f for f in data_files if not f.endswith(".c")]

    cmdclass["build_py"] = BuildPyExcludeSource

setup(
    name="artemis",
    version="3.6.3",
    packages=find_namespace_packages(
        include=[
            "artemis",
            "artemis.*",
            "apps",
            "apps.*",
            "admin_console",
            "admin_console.*",
        ]
    ),
    package_dir={
        "": ".",
        "admin_console": "apps/admin_console",
    },
    package_data={
        "artemis": ["**/*.json", "**/*.md"],
        "apps.admin_console": ["index.html", "**/*.json", "**/*.md"],
        "admin_console": ["index.html", "**/*.json", "**/*.md"],
    },
    include_package_data=True,
    ext_modules=ext_modules,
    cmdclass=cmdclass,
    exclude_package_data={"": ["*.c", "*.pyx", "*.pxd", "*.cpp"]},
)
