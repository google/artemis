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

"""Tests for object_detector universal tools."""

from artemis.tools.base import ArtemisTool
from artemis.tools.object_detection_tool import (
    ObjectDetection,
    ObjectDetectionArgs,
    ObjectDetectionTool,
    ObjectDetectorTool,
    OperatorObjectDetection,
    OperatorObjectDetectionArgs,
    OperatorObjectDetectionTool,
    OperatorObjectDetectorTool,
    object_detection,
    operator_object_detection,
)


def test_object_detector_tool_subclass():
    """Verify ObjectDetectionTool is an ArtemisTool subclass."""
    assert issubclass(ObjectDetectionTool, ArtemisTool)
    assert issubclass(ObjectDetection, ArtemisTool)
    assert issubclass(ObjectDetectorTool, ArtemisTool)
    assert issubclass(OperatorObjectDetectionTool, ArtemisTool)
    assert issubclass(OperatorObjectDetectionTool, ObjectDetectionTool)
    assert issubclass(OperatorObjectDetection, ArtemisTool)
    assert issubclass(OperatorObjectDetectorTool, ArtemisTool)

    assert isinstance(object_detection, ArtemisTool)
    assert isinstance(object_detection, ObjectDetectionTool)
    assert isinstance(operator_object_detection, ArtemisTool)
    assert isinstance(operator_object_detection, OperatorObjectDetectionTool)

    assert object_detection.name == "object_detection"
    assert object_detection.category == "perception"
    assert object_detection.args_schema == ObjectDetectionArgs

    assert operator_object_detection.name == "object_detection"
    assert operator_object_detection.category == "perception"
    assert operator_object_detection.args_schema == OperatorObjectDetectionArgs
