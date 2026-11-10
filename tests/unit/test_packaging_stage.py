"""Staging turns a raw Nuitka directory into a shippable bundle."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

PACKAGING = Path(__file__).resolve().parents[2] / "packaging"
sys.path.insert(0, str(PACKAGING))

import stage  # noqa: E402


def _fake_dist(root: Path, *, helper: bool = True, showcase: bool = True) -> Path:
    dist = root / "artemis_cli.dist"
    (dist / "artemis" / "resources").mkdir(parents=True)
    (dist / "config").mkdir()
    (dist / "config" / "artemis.jsonc").write_text("{}", encoding="utf-8")
    (dist / "rapidocr" / "models").mkdir(parents=True)
    (dist / "rapidocr" / "models" / "model.onnx").write_bytes(b"x")
    (dist / "licenses").mkdir()
    for index in range(stage.MINIMUM_LICENSE_FILES):
        (dist / "licenses" / f"dep-{index}.txt").write_text("text", encoding="utf-8")
    (dist / "licenses" / "FFMPEG.txt").write_text("gpl", encoding="utf-8")
    (dist / "LICENSE").write_text("apache", encoding="utf-8")
    (dist / "BUILD-INFO.txt").write_text("info", encoding="utf-8")
    if showcase:
        (dist / "artemis" / "resources" / "showcase_ui").mkdir()
    if helper:
        target = dist / "packages" / stage.HELPER_PACKAGE
        target.mkdir(parents=True)
        for name in stage.HELPER_FILES:
            (target / name).write_bytes(b"helper")
    return dist


def test_macos_exposes_the_command_as_a_symlink_to_the_suffixed_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    dist = _fake_dist(tmp_path)
    (dist / "artemis-app").write_bytes(b"binary")

    entry = stage.link_public_entry(dist)

    assert entry == dist / "bin" / "artemis"
    assert entry.is_symlink()
    assert entry.resolve() == (dist / "artemis-app").resolve()


def test_windows_exposes_the_same_relative_path_with_a_hardlink(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    dist = _fake_dist(tmp_path)
    (dist / "artemis.exe").write_bytes(b"binary")

    entry = stage.link_public_entry(dist)

    assert entry == dist / "bin" / "artemis.exe"
    assert entry.read_bytes() == b"binary"


def test_staging_is_repeatable(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    dist = _fake_dist(tmp_path)
    (dist / "artemis-app").write_bytes(b"binary")

    stage.link_public_entry(dist)
    entry = stage.link_public_entry(dist)

    assert entry.is_symlink()


def test_verify_reports_each_missing_piece(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    dist = _fake_dist(tmp_path, helper=False)
    (dist / "artemis-app").write_bytes(b"binary")
    stage.link_public_entry(dist)
    (dist / "BUILD-INFO.txt").unlink()

    failures = stage.verify(dist, smoke=False)

    assert any("helper file" in failure for failure in failures)
    assert any("BUILD-INFO" in failure for failure in failures)


def test_verify_accepts_a_complete_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    dist = _fake_dist(tmp_path, showcase=False)
    (dist / "artemis-app").write_bytes(b"binary")
    stage.link_public_entry(dist)

    assert stage.verify(dist, smoke=False) == []


def test_verify_rejects_the_frontend_the_scripts_remove(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    dist = _fake_dist(tmp_path)
    (dist / "artemis-app").write_bytes(b"binary")
    stage.link_public_entry(dist)

    failures = stage.verify(dist, smoke=False)

    assert any("frontend" in failure for failure in failures)


def test_build_info_names_the_commit_and_the_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    dist = _fake_dist(tmp_path)

    written = stage.write_build_info(dist)

    text = written.read_text(encoding="utf-8")
    assert "commit:" in text
    assert f"executable: {stage.real_executable_name()}" in text


def test_the_two_scripts_both_hand_the_dist_to_the_shared_stager():
    for script in ("build.sh", "build.ps1"):
        text = (PACKAGING / script).read_text(encoding="utf-8")
        assert "packaging/stage.py" in text, script


def test_the_scripts_no_longer_stage_by_hand():
    bash = (PACKAGING / "build.sh").read_text(encoding="utf-8")
    powershell = (PACKAGING / "build.ps1").read_text(encoding="utf-8")

    for text in (bash, powershell):
        assert "showcase_ui" not in text
        assert "collect_licenses" not in text
        assert "helper_manifest" not in text


def test_main_exits_non_zero_when_a_bundle_is_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    dist = _fake_dist(tmp_path, helper=False)

    code = stage.main(["stage.py", str(dist), "--skip-smoke"])

    assert code == 1
