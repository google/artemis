"""The Showcase UI row must not offer a source-tree fix inside a bundle."""

from unittest.mock import patch

import pytest

from artemis.interfaces.cli.commands import doctor


@pytest.fixture
def no_showcase_dist(monkeypatch):
    monkeypatch.setattr(doctor, "ROOT_DIR", __import__("pathlib").Path("/nonexistent"))
    monkeypatch.setattr("artemis.resources.get_bundled_showcase_dist", lambda: None)


def test_bundle_does_not_point_at_a_build_script_it_does_not_carry(no_showcase_dist):
    with patch("artemis.interfaces.cli.commands.doctor.is_frozen_bundle", return_value=True):
        row = doctor._showcase_row()

    assert row.summary == "Not Shipped"
    assert "API only" in row.detail
    assert "auto-compile" not in row.detail


def test_source_checkout_keeps_the_build_hint(no_showcase_dist):
    with patch("artemis.interfaces.cli.commands.doctor.is_frozen_bundle", return_value=False):
        row = doctor._showcase_row()

    assert row.summary == "Not Compiled"
    assert "auto-compile" in row.detail
