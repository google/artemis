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

"""Tests for the local model catalog and the `artemis model` CLI surface."""

from typer.testing import CliRunner

from artemis.config.local_models import (
    DEFAULT_LOCAL_MODEL,
    LOCAL_MODELS,
    resolve_model_ref,
)
from artemis.interfaces.cli.commands.model import model_app

runner = CliRunner()


class TestResolveModelRef:
    def test_catalog_alias(self):
        assert resolve_model_ref("gemma4-e4b") == (
            "gemma4-e4b",
            LOCAL_MODELS["gemma4-e4b"].repo,
        )

    def test_default_alias_is_cataloged(self):
        assert DEFAULT_LOCAL_MODEL in LOCAL_MODELS

    def test_repo_id_passthrough(self):
        assert resolve_model_ref("org/some-model") == (
            "org/some-model",
            "org/some-model",
        )

    def test_unknown_bare_name_rejected(self):
        assert resolve_model_ref("not-a-model") is None


class TestModelCli:
    def test_list_shows_catalog(self):
        result = runner.invoke(model_app, ["list"])
        assert result.exit_code == 0
        assert "gemma4-e4b" in result.output

    def test_pull_rejects_unknown_alias(self):
        result = runner.invoke(model_app, ["pull", "bogus-name"])
        assert result.exit_code == 1
        assert "Unknown model" in result.output

    def test_serve_rejects_unknown_alias(self):
        result = runner.invoke(model_app, ["serve", "bogus-name"])
        assert result.exit_code == 1
        assert "Unknown model" in result.output

    def test_stop_without_state_is_clean(self, tmp_path, monkeypatch):
        import artemis.interfaces.cli.commands.model as model_mod

        monkeypatch.setattr(model_mod, "_STATE_DIR", tmp_path)
        result = runner.invoke(model_app, ["stop", "--port", "19191"])
        assert result.exit_code == 0
        assert "No managed server" in result.output


class TestVisionSoftTokens:
    def test_catalog_default(self):
        from artemis.config.local_models import LOCAL_MODELS

        assert LOCAL_MODELS["gemma4-e4b"].vision_soft_tokens == 1120

    def test_apply_writes_processor_config(self, tmp_path, monkeypatch):
        import json as _json

        import artemis.interfaces.cli.commands.model as model_mod

        snap = tmp_path / "snap"
        snap.mkdir()
        (snap / "processor_config.json").write_text(
            _json.dumps({"image_processor": {"max_soft_tokens": 280}})
        )
        monkeypatch.setattr(model_mod, "_snapshot_path", lambda repo: snap)
        assert model_mod._apply_vision_soft_tokens("any/repo", 1120)
        cfg = _json.loads((snap / "processor_config.json").read_text())
        assert cfg["image_processor"]["max_soft_tokens"] == 1120

    def test_apply_missing_key_is_noop(self, tmp_path, monkeypatch):
        import json as _json

        import artemis.interfaces.cli.commands.model as model_mod

        snap = tmp_path / "snap"
        snap.mkdir()
        (snap / "processor_config.json").write_text(_json.dumps({"other": {}}))
        monkeypatch.setattr(model_mod, "_snapshot_path", lambda repo: snap)
        assert not model_mod._apply_vision_soft_tokens("any/repo", 1120)

    def test_serve_rejects_bad_token_value(self):
        result = runner.invoke(model_app, ["serve", "--vision-tokens", "999"])
        assert result.exit_code == 1
        assert "vision-tokens" in result.output
