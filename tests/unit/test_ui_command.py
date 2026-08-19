# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

import os

from artemis.interfaces.cli.commands.ui import _showcase_build_required


def test_showcase_build_required_when_source_is_newer(tmp_path):
    showcase_dir = tmp_path / "showcase_ui"
    source_file = showcase_dir / "src" / "app" / "agent.service.ts"
    built_index = showcase_dir / "dist" / "frontend" / "browser" / "index.html"
    source_file.parent.mkdir(parents=True)
    built_index.parent.mkdir(parents=True)
    source_file.write_text("source", encoding="utf-8")
    built_index.write_text("build", encoding="utf-8")

    os.utime(built_index, (100, 100))
    os.utime(source_file, (200, 200))

    assert _showcase_build_required(showcase_dir) is True


def test_showcase_build_not_required_when_build_is_current(tmp_path):
    showcase_dir = tmp_path / "showcase_ui"
    source_file = showcase_dir / "src" / "app" / "agent.service.ts"
    built_index = showcase_dir / "dist" / "frontend" / "browser" / "index.html"
    source_file.parent.mkdir(parents=True)
    built_index.parent.mkdir(parents=True)
    source_file.write_text("source", encoding="utf-8")
    built_index.write_text("build", encoding="utf-8")

    os.utime(source_file, (100, 100))
    os.utime(built_index, (200, 200))

    assert _showcase_build_required(showcase_dir) is False
