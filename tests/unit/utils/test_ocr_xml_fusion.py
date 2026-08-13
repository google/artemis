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

import base64
from io import BytesIO
import unittest
from artemis.utils.ocr_xml_fusion import (
    _calculate_overlap_ratio,
    _crop_image_remove_status_bar,
    _detect_status_bar_height,
    _is_low_value_text,
    _map_coordinates_back,
    _parse_ocr_position,
    fuse_ocr_with_xml,
)
from PIL import Image


class TestOCRFusion(unittest.TestCase):
    def test_is_low_value_text(self):
        # Test length 1
        self.assertTrue(_is_low_value_text("a"))
        self.assertFalse(_is_low_value_text("Yes"))
        self.assertFalse(_is_low_value_text("1"))

        # Test placeholders
        self.assertTrue(_is_low_value_text("search"))

        # Test default markers
        self.assertTrue(_is_low_value_text("[Icon]"))

        # Test valid text
        self.assertFalse(_is_low_value_text("Click Me"))

    def test_parse_ocr_position(self):
        position = [
            {"x": 10, "y": 20},
            {"x": 50, "y": 20},
            {"x": 50, "y": 60},
            {"x": 10, "y": 60},
        ]
        bounds = _parse_ocr_position(position)
        self.assertEqual(bounds, {"left": 10, "top": 20, "right": 50, "bottom": 60})

    def test_calculate_overlap_ratio(self):
        ocr_bounds = {"left": 10, "top": 10, "right": 30, "bottom": 30}
        xml_bounds = {"left": 0, "top": 0, "right": 40, "bottom": 40}
        # OCR is fully inside XML
        self.assertEqual(_calculate_overlap_ratio(ocr_bounds, xml_bounds), 1.0)

        xml_bounds = {"left": 20, "top": 20, "right": 40, "bottom": 40}
        # Intersection is 10x10 = 100. OCR area is 20x20 = 400. Ratio = 0.25
        self.assertEqual(_calculate_overlap_ratio(ocr_bounds, xml_bounds), 0.25)

    def test_detect_status_bar_height(self):
        xml = [
            {"package": "com.android.systemui", "bounds": "[0,0][1080,120]"},
            {"package": "com.example.app", "bounds": "[0,120][1080,2400]"},
        ]
        # Pass screen_height=2400, should find 120 from XML
        self.assertEqual(_detect_status_bar_height(xml, screen_height=2400), 120)

        # Fallback: should be 4% of 2400 = 96
        xml = [{"package": "com.example.app", "bounds": "[0,0][1080,2400]"}]
        self.assertEqual(_detect_status_bar_height(xml, screen_height=2400), 96)

    def test_crop_image_remove_status_bar(self):
        # Create a dummy image
        img = Image.new("RGB", (100, 100), color="red")
        buffered = BytesIO()
        img.save(buffered, format="PNG")
        img_b64 = base64.b64encode(buffered.getvalue()).decode("utf-8")

        cropped_b64, w, h = _crop_image_remove_status_bar(img_b64, crop_height=20)
        self.assertEqual(w, 100)
        self.assertEqual(h, 100)

        # Verify cropped size
        cropped_img = Image.open(BytesIO(base64.b64decode(cropped_b64)))
        self.assertEqual(cropped_img.size, (100, 80))

    def test_map_coordinates_back(self):
        ocr_results = [
            {
                "text": "test",
                "position": [{"x": 10, "y": 10}, {"x": 20, "y": 20}],
            }
        ]
        mapped = _map_coordinates_back(ocr_results, status_bar_height=100)
        self.assertEqual(mapped[0]["position"][0]["y"], 110)
        self.assertEqual(mapped[0]["position"][1]["y"], 120)

    def test_fuse_ocr_with_xml(self):
        xml = [
            {
                "class": "android.widget.Button",
                "bounds": "[10,100][200,150]",
                "text": "Submit",
            },
            {
                "class": "android.widget.TextView",
                "bounds": "[10,200][200,250]",
                "text": "",
            },
        ]
        ocr = [
            # Should be discarded (similar to Submit)
            {
                "text": "Submit",
                "position": [{"x": 15, "y": 105}, {"x": 95, "y": 145}],
            },
            # Multiple OCRs matching the same node (TextView at [10,200])
            # They are on the same line, should be merged in order
            {
                "text": "Result",
                "position": [{"x": 15, "y": 205}, {"x": 95, "y": 245}],
            },
            {
                "text": "Found",
                "position": [{"x": 105, "y": 205}, {"x": 185, "y": 245}],
            },
            # Unmatched OCRs that are close to each other (should be merged into one virtual node)
            {
                "text": "Floating",
                "position": [{"x": 15, "y": 305}, {"x": 95, "y": 345}],
            },
            {
                "text": "Text",
                "position": [{"x": 105, "y": 305}, {"x": 185, "y": 345}],
            },
            # Unmatched OCR far away (should be separate virtual node)
            {
                "text": "Far",
                "position": [{"x": 15, "y": 405}, {"x": 95, "y": 445}],
            },
        ]

        fused = fuse_ocr_with_xml(xml, ocr)

        # Check first node (Submit) - should NOT have ocr_text
        self.assertNotIn("ocr_text", fused[0])

        # Check second node (Result Found) - should have aggregated ocr_elements
        self.assertEqual(fused[1]["ocr_elements"][0]["text"], "Result Found")
        self.assertIn("[OCR]", fused[1]["class"])

        # We expect 0 virtual nodes now as per new strategy.
        # Total nodes should be just the original 2 XML nodes.
        self.assertEqual(len(fused), 2)

    def test_banded_overlap_matching(self):
        # Screen is 1080x2400
        # XML has a giant container and a small correct button.
        xml = [
            # Giant container (FrameLayout, covers a lot of screen)
            {
                "class": "android.widget.FrameLayout",
                "bounds": "[0,80][1080,1000]",
                "text": "",
            },
            # Small correct button
            {
                "class": "android.widget.Button",
                "bounds": "[185,84][512,114]",
                "text": "",
            },
        ]

        # OCR Search is slightly shifted to the left of the Button.
        # Button: [185,84][512,114] -> area 327 * 30 = 9810
        # OCR: [174,89][326,109] -> area 152 * 20 = 3040
        # Overlap with Button:
        #   Intersection: [185,89][326,109] -> area 141 * 20 = 2820
        #   Ratio: 2820 / 3040 = 0.927 (Band 1)
        # Overlap with FrameLayout:
        #   OCR is fully inside FrameLayout [0,80][1080,1000]
        #   Ratio: 1.0 (Band 1)
        # Both are in Band 1 (>=0.9).
        # The Button has MUCH smaller area than FrameLayout (9810 < 993600).
        # So Button should win despite having lower overlap (0.927 < 1.0).
        ocr = [
            {
                "text": "Search",
                "position": [
                    {"x": 174, "y": 89},
                    {"x": 326, "y": 89},
                    {"x": 326, "y": 109},
                    {"x": 174, "y": 109},
                ],
            }
        ]

        fused = fuse_ocr_with_xml(xml, ocr)

        # Check that OCR text "Search" is mounted on the Button node (index 1)
        self.assertIn("ocr_elements", fused[1])
        self.assertEqual(fused[1]["ocr_elements"][0]["text"], "Search")

        # And NOT on the FrameLayout node (index 0)
        self.assertNotIn("ocr_elements", fused[0])

    def test_giant_node_exclusion(self):
        # Screen is 1080x2400. Area = 2,592,000.
        # 10% area = 259,200.
        # FrameLayout is 1080x1000 -> area 1,080,000 (>10%).
        # It should be excluded from matching entirely.
        # Since there is no other node, the OCR should be discarded.
        xml = [
            {
                "class": "android.widget.FrameLayout",
                "bounds": "[0,0][1080,1000]",
                "text": "",
            },
        ]
        ocr = [
            # Some floating developer text "dX : 0.0"
            {
                "text": "dX : 0.0",
                "position": [
                    {"x": 144, "y": 55},
                    {"x": 215, "y": 55},
                    {"x": 215, "y": 63},
                    {"x": 144, "y": 63},
                ],
            }
        ]

        fused = fuse_ocr_with_xml(xml, ocr)

        # The giant FrameLayout should NOT have ocr_elements
        self.assertNotIn("ocr_elements", fused[0])
        # The class should NOT be updated with [OCR]
        self.assertNotIn("[OCR]", fused[0].get("class", ""))


if __name__ == "__main__":
    unittest.main()
