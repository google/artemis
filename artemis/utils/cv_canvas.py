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

import json
from pathlib import Path
import cv2


class ImageCanvas:
    def __init__(self, img_id: str, intermediate_dir: str):
        self.intermediate_dir = intermediate_dir

        pool_path = Path(intermediate_dir) / "intermediate_transforms.json"
        if not pool_path.exists():
            raise ValueError(f"Image pool registry file not found at {pool_path}")

        with open(pool_path) as f:
            pool = json.load(f)

        if img_id not in pool:
            raise ValueError(f"Image ID '{img_id}' not found in the intermediate image pool.")

        entry = pool[img_id]
        img_path = entry["path"]

        self.img = cv2.imread(img_path)
        if self.img is None:
            raise ValueError(f"Failed to read image at {img_path}")
        self.width, self.height = self._get_resolution()
        self.transform = entry["transform"].copy()
        self.annotations = {}

    def _get_resolution(self) -> tuple[int, int]:
        height, width = self.img.shape[:2]
        return width, height

    def normalized_to_pixel_coords(self, x: float, y: float) -> tuple[int, int]:
        """Convert normalized (0-1000) coordinates to pixel coordinates."""
        px = int(round(x * self.width / 1000.0))
        py = int(round(y * self.height / 1000.0))
        px = max(0, min(self.width - 1, px))
        py = max(0, min(self.height - 1, py))
        return px, py

    def draw_dot(self, x: int, y: int, label_idx: int, radius: int = 10) -> "ImageCanvas":
        cx, cy = x, y
        # Draw nested white and red circles
        cv2.circle(self.img, (cx, cy), 2 * radius, (255, 255, 255), -1)
        cv2.circle(self.img, (cx, cy), radius, (0, 0, 255), -1)

        # Draws text "V{label_idx}" using cv2.putText with PIL-matching offset: (x + 2*radius + 4, y - 22)
        tx = int(x + 2 * radius + 4)
        ty = int(y - 22)
        text = f"V{label_idx}"

        cv2.putText(
            self.img,
            text,
            (tx, ty),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.5,
            (255, 255, 255),
            10,
            cv2.LINE_AA,
        )
        cv2.putText(
            self.img,
            text,
            (tx, ty),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.5,
            (0, 0, 255),
            3,
            cv2.LINE_AA,
        )

        self.annotations[text] = [cx, cy]
        return self

    def save(self, final: bool) -> None:
        pool_json_path = Path(self.intermediate_dir) / "intermediate_transforms.json"
        if pool_json_path.exists():
            with open(pool_json_path) as f:
                pool = json.load(f)
        else:
            pool = {}

        # Auto-generate next sequential image_id (e.g. "img_1")
        img_nums = []
        for k in pool.keys():
            if k.startswith("img_"):
                try:
                    img_nums.append(int(k[4:]))
                except ValueError:
                    pass
        next_num = max(img_nums) + 1 if img_nums else 1
        image_id = f"img_{next_num}"

        out_dir = Path(self.intermediate_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{image_id}.jpg"

        success = cv2.imwrite(str(out_path), self.img)
        if not success:
            raise ValueError(f"Failed to save image to {out_path}")

        pool[image_id] = {
            "image_id": image_id,
            "path": str(out_path.resolve()),
            "transform": self.transform.copy(),
            "is_output": final,
            "annotations": self.annotations.copy(),
        }

        with open(pool_json_path, "w") as f:
            json.dump(pool, f, indent=2)

    def crop(self, x: int, y: int, w: int, h: int):
        if w <= 0 or h <= 0:
            raise ValueError("Width and height must be strictly positive.")

        # Calculate bounding boxes
        orig_h, orig_w = self.img.shape[:2]
        x_end = min(x + w, orig_w)
        y_end = min(y + h, orig_h)
        x = max(0, x)
        y = max(0, y)

        self.img = self.img[y:y_end, x:x_end]
        self.width, self.height = self._get_resolution()

        # Adjust offsets by considering the current scale factor
        self.transform["offset_x"] += x / self.transform["scale_x"]
        self.transform["offset_y"] += y / self.transform["scale_y"]

        # Transform coordinates inside self.annotations
        new_annotations = {}
        new_w = x_end - x
        new_h = y_end - y
        for label, coord in self.annotations.items():
            px, py = coord
            new_px = px - x
            new_py = py - y
            if 0 <= new_px < new_w and 0 <= new_py < new_h:
                new_annotations[label] = [int(new_px), int(new_py)]
        self.annotations = new_annotations

        return self

    def resize_by_factor(self, factor_x: float, factor_y: float = None):
        if factor_x <= 0:
            raise ValueError("Scale factor must be strictly positive.")
        if factor_y is None:
            factor_y = factor_x

        height, width = self.img.shape[:2]
        new_width = int(width * factor_x)
        new_height = int(height * factor_y)

        if new_width == 0 or new_height == 0:
            raise ValueError("Resulting dimensions must be greater than 0.")

        self.img = cv2.resize(self.img, (new_width, new_height))
        self.width, self.height = self._get_resolution()
        self.transform["scale_x"] *= factor_x
        self.transform["scale_y"] *= factor_y

        # Transform coordinates inside self.annotations
        new_annotations = {}
        for label, coord in self.annotations.items():
            px, py = coord
            new_px = int(px * factor_x)
            new_py = int(py * factor_y)
            if 0 <= new_px < new_width and 0 <= new_py < new_height:
                new_annotations[label] = [new_px, new_py]
        self.annotations = new_annotations

        return self
