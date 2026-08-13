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

from pathlib import Path
from unittest.mock import MagicMock

from artemis.context import ArtemisContext
from artemis.data_engine.engine import DataEngine
import pytest

INPUTS_DIR = Path(__file__).parent / "inputs"


@pytest.fixture(scope="session")
def inputs_dir():
    return INPUTS_DIR


@pytest.fixture
def mock_adb_client():
    client = MagicMock()
    # Dummy outputs for commands and physical interactions that tools might use
    client.shell.return_value = "dummy adb output"
    # Basic UI hierarchy for mobile read_hierarchy tool
    client.get_hierarchy.return_value = (
        "<?xml version='1.0' encoding='UTF-8'?><hierarchy><node index='0'"
        " text='dummy'/></hierarchy>"
    )
    return client


@pytest.fixture
def artemis_context(inputs_dir, mock_adb_client):
    """Provides a ArtemisContext with loaded database and actual side-effects,

    except the ADB connection is customized to read from fixtures instead of a
    real device.
    """
    ctx = MagicMock(spec=ArtemisContext)

    ctx.execution_setup = MagicMock()
    ctx.execution_setup.traces_path = str(inputs_dir)

    # Initialize real DataEngine if the fixture database exists
    db_path = inputs_dir / "data_engine.db"
    if db_path.exists():
        engine = DataEngine(ctx)
        engine.start_session(goal="Test goal")
        import uuid

        engine.current_step_id = uuid.uuid4()
        ctx.data_engine = engine
    else:
        ctx.data_engine = None

    ctx.ui_adb_client = mock_adb_client

    # Fake device representation
    ctx.device = MagicMock()
    ctx.device.device_width = 1080
    ctx.device.device_height = 2400

    # Trace output directory setup to point inside inputs or a dedicated test folder
    trace_out = inputs_dir / "trace_output"
    trace_out.mkdir(exist_ok=True)
    ctx.trace_dir = trace_out

    ctx.agent_config = MagicMock()
    ctx.agent_config.denylisted_tools = {}

    return ctx


@pytest.fixture
def mock_state(inputs_dir):
    """Provides a dummy graph State with realistic artifact paths for tools that need state injection."""
    from artemis.graph.state import State

    state = MagicMock(spec=State)
    state.latest_screenshot = str(inputs_dir / "screenshot.jpg")
    state.video_path = str(inputs_dir / "recording.mp4")
    state.indexed_points = []
    state.indexed_elements = []
    return state
