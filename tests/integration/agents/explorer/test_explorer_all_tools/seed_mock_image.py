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

import hashlib
import json
from pathlib import Path
import sqlite3


def seed_database():
    db_path = Path(__file__).resolve().parents[5] / "traces" / "data_engine.db"
    screenshot_path = (
        Path(__file__).resolve().parent
        / "input_screenshot_test_explorer_all_tools_sequential_mocked.jpg"
    )

    if not screenshot_path.exists():
        # Try fallback to tests/screenshots/pixel_phone_mockup.png
        source_path = (
            Path(__file__).resolve().parents[5] / "tests" / "screenshots" / "pixel_phone_mockup.png"
        )
        import shutil

        shutil.copy(source_path, screenshot_path)
        print(f"Copied screenshot from {source_path} to {screenshot_path}")

    # Compute SHA-256 hash
    sha256_hash = hashlib.sha256()
    with open(screenshot_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    computed_hash = sha256_hash.hexdigest()
    print(f"Computed screenshot hash: {computed_hash}")

    # Bounding elements
    # 1. Settings [100, 200][300, 250] -> center pixel [200, 225] -> normalized [185, 93]
    # 2. Dashboard [400, 400][600, 600] -> center pixel [500, 500] -> normalized [462, 208]
    # 3. Search [800, 100][950, 180] -> center pixel [875, 140] -> normalized [810, 58]
    # 4. Profile [300, 800][450, 850] -> center pixel [375, 825] -> normalized [347, 343]
    # 5. Back [50, 50][150, 100] -> center pixel [100, 75] -> normalized [92, 31]

    ocr_result = [
        {"text": "Screen Dashboard Title", "position": []},
        {
            "text": "Settings",
            "position": [
                {"x": 100, "y": 200},
                {"x": 300, "y": 200},
                {"x": 300, "y": 250},
                {"x": 100, "y": 250},
            ],
        },
        {
            "text": "Dashboard",
            "position": [
                {"x": 400, "y": 400},
                {"x": 600, "y": 400},
                {"x": 600, "y": 600},
                {"x": 400, "y": 600},
            ],
        },
        {
            "text": "Search",
            "position": [
                {"x": 800, "y": 100},
                {"x": 950, "y": 100},
                {"x": 950, "y": 180},
                {"x": 800, "y": 180},
            ],
        },
        {
            "text": "Profile",
            "position": [
                {"x": 300, "y": 800},
                {"x": 450, "y": 800},
                {"x": 450, "y": 850},
                {"x": 300, "y": 850},
            ],
        },
        {
            "text": "Back",
            "position": [
                {"x": 50, "y": 50},
                {"x": 150, "y": 50},
                {"x": 150, "y": 100},
                {"x": 50, "y": 100},
            ],
        },
    ]

    ui_tree = [
        {"class": "android.widget.FrameLayout", "bounds": "[0,0][1080,2400]"},
        {
            "class": "android.widget.Button",
            "text": "Settings",
            "resource-id": "com.example.app:id/settings",
            "clickable": "true",
            "bounds": "[100,200][300,250]",
        },
        {
            "class": "android.widget.Button",
            "text": "Dashboard",
            "resource-id": "com.example.app:id/dashboard",
            "clickable": "true",
            "bounds": "[400,400][600,600]",
        },
        {
            "class": "android.widget.Button",
            "text": "Search",
            "resource-id": "com.example.app:id/search",
            "clickable": "true",
            "bounds": "[800,100][950,180]",
        },
        {
            "class": "android.widget.Button",
            "text": "Profile",
            "resource-id": "com.example.app:id/profile",
            "clickable": "true",
            "bounds": "[300,800][450,850]",
        },
        {
            "class": "android.widget.Button",
            "text": "Back",
            "resource-id": "com.example.app:id/back",
            "clickable": "true",
            "bounds": "[50,50][150,100]",
        },
    ]

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute(
        "INSERT OR REPLACE INTO images (image_name, timestamp, ocr_result,"
        " ui_tree, extra_metadata) VALUES (?, ?, ?, ?, ?)",
        (
            computed_hash,
            1717398000.0,
            json.dumps(ocr_result),
            json.dumps(ui_tree),
            json.dumps({}),
        ),
    )

    conn.commit()
    conn.close()
    print(f"Successfully inserted mock image record {computed_hash} into database at {db_path}")


if __name__ == "__main__":
    seed_database()
