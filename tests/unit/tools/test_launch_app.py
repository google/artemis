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

from unittest.mock import MagicMock, patch
from artemis.agents.hopper.hopper import HopperOutput
from artemis.context import ArtemisContext
from artemis.tools.mobile.launch_app import find_package
import pytest


@pytest.fixture
def mock_context():
    ctx = MagicMock(spec=ArtemisContext)
    ctx.package_cache = {}
    return ctx


@pytest.mark.asyncio
@patch("artemis.tools.mobile.launch_app.list_packages_async")
@patch("artemis.tools.mobile.launch_app.hopper")
async def test_find_package_cache_hit(mock_hopper, mock_list_packages, mock_context):
    mock_list_packages.return_value = "com.example.app1\ncom.example.app2"

    # First call: not in cache, calls hopper
    mock_hopper.return_value = HopperOutput(
        found=True, output="com.example.app1", reason="Found it"
    )

    pkg1 = await find_package(mock_context, "App 1")
    assert pkg1 == "com.example.app1"
    assert mock_hopper.call_count == 1
    assert mock_context.package_cache["App 1"] == "com.example.app1"

    # Second call: cache hit, does not call hopper or list_packages again
    mock_hopper.reset_mock()
    mock_list_packages.reset_mock()

    pkg2 = await find_package(mock_context, "App 1")
    assert pkg2 == "com.example.app1"
    mock_hopper.assert_not_called()
    mock_list_packages.assert_not_called()


@pytest.mark.asyncio
@patch("artemis.tools.mobile.launch_app.list_packages_async")
@patch("artemis.tools.mobile.launch_app.hopper")
async def test_find_package_not_found_caches_none(mock_hopper, mock_list_packages, mock_context):
    mock_list_packages.return_value = "com.example.app1\ncom.example.app2"

    # Hopper doesn't find it
    mock_hopper.return_value = HopperOutput(found=False, output=None, reason="Not found")

    pkg1 = await find_package(mock_context, "App 3")
    assert pkg1 is None
    assert mock_hopper.call_count == 1
    assert mock_context.package_cache["App 3"] is None

    # Second call: cache hit (None), does not call hopper again
    mock_hopper.reset_mock()
    pkg2 = await find_package(mock_context, "App 3")
    assert pkg2 is None
    mock_hopper.assert_not_called()


@pytest.mark.asyncio
@patch("artemis.tools.mobile.launch_app.list_packages_async")
@patch("artemis.tools.mobile.launch_app.hopper")
async def test_find_package_passes_use_fallback(mock_hopper, mock_list_packages, mock_context):
    mock_list_packages.return_value = "com.example.app1\ncom.example.app2"
    mock_hopper.return_value = HopperOutput(
        found=True, output="com.example.app1", reason="Found it"
    )

    # Pass use_fallback=False
    await find_package(mock_context, "App 1", use_fallback=False)
    mock_hopper.assert_called_once_with(
        ctx=mock_context,
        request=("I'm looking for the package name of the following app: 'App 1'"),
        data="com.example.app1\ncom.example.app2",
        use_fallback=False,
    )
