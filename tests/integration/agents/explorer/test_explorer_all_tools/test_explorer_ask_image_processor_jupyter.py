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

import asyncio
import json
import logging
from pathlib import Path
import shutil
from unittest.mock import patch

from artemis.config import settings
from artemis.agents.image_processor.image_processor import ImageProcessor
from tests.integration.agents.explorer.test_explorer_all_tools.helpers import (
    create_mock_context,
    get_or_create_test_screenshot,
)
from artemis.utils.python_executor import PythonExecutor
import pytest

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_explorer_ask_image_processor_jupyter")


@pytest.mark.asyncio
async def test_explorer_ask_image_processor_jupyter():
    test_name = "test_explorer_ask_image_processor_jupyter"

    # 1. Setup outputs directory
    outputs_dir = Path(__file__).resolve().parent / "outputs" / test_name
    outputs_dir.mkdir(parents=True, exist_ok=True)

    # Redirect traces path to the new outputs directory
    settings.TRACES_PATH = outputs_dir

    # 2. Prepare the input image
    src_image_path = get_or_create_test_screenshot()
    input_screenshot = outputs_dir / "input_screenshot.jpg"
    shutil.copy2(src_image_path, input_screenshot)
    logger.info(f"Copied input screenshot to: {input_screenshot}")

    # 3. Formulate the instruction with the injected G-Logo coordinate prior
    instruction = (
        'The user wants to find "the red patch above the green patch". Let\'s'
        " analyze the screen layout. \nIn the dock / app icons, we have:\n-"
        " Play Store (9): colorful triangle (has green, blue, yellow, red)\n-"
        " Gmail (10): M icon (red, yellow, green, blue)\n- Photos (11):"
        " pinwheel (red, yellow, green, blue)\n- Chrome (16): circular logo"
        " with green on the bottom, red on top, yellow on the side, blue"
        " center.\nWait, let's look at the Photos icon (11) or Chrome icon (16)"
        " or Gmail (10) or Google search bar logo (21) or Play Store"
        " (9).\nLet's zoom into Chrome (16), Gmail (10), Photos (11), Play"
        " Store (9), Google logo (21) to analyze the relative positions of red"
        " patches and green patches.\nThe G-Logo is exactly at pixel"
        " coordinates [2173, 107, 2241, 174].\nLet's crop these icons to find"
        ' which icon has a "red patch above the green patch" or is it a'
        " specific patch?\nWait!\nLet's look at Chrome (16):\nRed is at the"
        " top, green is at the bottom/left. Red is directly above green? Yes,"
        " the top slice of Chrome is red, and the bottom slice is green.\nLet's"
        " look at Photos (11):\nIt has four petals. The top petal is red? No,"
        " top petal is red/pink, bottom is green? Let's check.\nLet's look at"
        " Gmail (10):\nThe red is the M shape, green is at the bottom"
        " right?\nLet's write Python code to crop these areas and output the"
        " crop to see clearly.\nSpecifically, let's crop:\n- Play Store (bounds"
        " around 9)\n- Gmail (bounds around 10)\n- Photos (bounds around 11)\n-"
        " Chrome (bounds around 16)\n- Google widget logo (bounds around"
        " 21)\nWe will save a visualization of these cropped regions side by"
        " side or separately so we can examine the colors."
    )

    # 4. Intercept Python executions to record them for the Jupyter Notebook
    executed_turns = []
    original_execute = PythonExecutor.execute

    def wrapped_execute(self, code: str) -> str:
        logger.info(f"Intercepting Python code execution. Code:\n{code}\n")
        result = original_execute(self, code)
        logger.info(f"Execution result:\n{result}\n")
        executed_turns.append({"code": code, "output": result})
        return result

    from langchain_core.messages import AIMessage
    from unittest.mock import AsyncMock, MagicMock

    sample_code = """
from artemis.utils.cv_canvas import ImageCanvas
import glob
from pathlib import Path
from artemis.config import settings

dirs = sorted(glob.glob(str(Path(settings.TRACES_PATH) / "images" / "image_processor" / "intermediates_*")))
intermediate_dir = dirs[-1] if dirs else "."

canvas = ImageCanvas("img_0", intermediate_dir)
canvas.crop(10, 10, 50, 50)
canvas.save(final=True)
"""

    mock_llm = MagicMock()
    mock_bound = MagicMock()
    mock_bound.ainvoke = AsyncMock(
        side_effect=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "execute_python",
                        "args": {"code": sample_code},
                        "id": "call_1",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "submit_result",
                        "args": {"summary": "Cropped target G-Logo region."},
                        "id": "call_2",
                    }
                ],
            ),
        ]
    )
    mock_llm.bind_tools.return_value = mock_bound

    mock_ctx = create_mock_context()
    image_processor = ImageProcessor(mock_ctx)

    # Run with patched executor and mock LLM
    logger.info("Running Image Processor with G-Logo prior...")
    with (
        patch.object(PythonExecutor, "execute", wrapped_execute),
        patch(
            "artemis.agents.image_processor.image_processor.get_llm",
            return_value=mock_llm,
        ),
    ):
        result = await image_processor.run(instruction, str(input_screenshot))

    logger.info(f"Image Processor finished. Result: {result}")

    # 5. Compile and save the Jupyter Notebook (.ipynb)
    notebook_cells = []
    # Add a markdown introduction cell
    notebook_cells.append(
        {
            "cell_type": "markdown",
            "metadata": {},
            "source": [
                "# Image Processor Execution Trace\n",
                f"**Task**: {instruction[:150]}...\n",
                "**Input Image**: `input_screenshot.jpg`",
            ],
        }
    )

    for turn_idx, turn in enumerate(executed_turns):
        # Add code cell
        notebook_cells.append(
            {
                "cell_type": "code",
                "execution_count": turn_idx + 1,
                "metadata": {},
                "outputs": [
                    {
                        "name": "stdout",
                        "output_type": "stream",
                        "text": [line + "\n" for line in turn["output"].split("\n")],
                    }
                ],
                "source": [line + "\n" for line in turn["code"].split("\n")],
            }
        )

    # If a result was submitted, add a final markdown cell
    outputs = result.get("outputs", [])
    if outputs:
        final_image_path = outputs[-1]["path"]
        notebook_cells.append(
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [
                    "# 🎉 Execution Completed Successfully\n",
                    f"**Summary**: {result.get('summary')}\n",
                    f"**Output Image**: `{Path(final_image_path).name}`",
                ],
            }
        )
        # Copy final image to output directory root for easy access
        shutil.copy2(final_image_path, outputs_dir / "final_output.jpg")

    notebook_json = {
        "cells": notebook_cells,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            }
        },
        "nbformat": 4,
        "nbformat_minor": 2,
    }

    notebook_path = outputs_dir / "vision_coder_execution.ipynb"
    with open(notebook_path, "w", encoding="utf-8") as f:
        json.dump(notebook_json, f, indent=2)

    logger.info(f"Saved compiled Jupyter notebook to: {notebook_path}")

    # 6. Assertions
    assert "error" not in result, f"Image Processor failed: {result.get('error')}"
    assert "outputs" in result, "Image Processor did not return outputs"
    assert len(result["outputs"]) > 0, "Image Processor did not return any output images"
    logger.info("Test completed successfully! All assertions passed.")


if __name__ == "__main__":
    asyncio.run(test_explorer_ask_image_processor_jupyter())
