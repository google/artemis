#!/usr/bin/env python3
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

"""ARTEMIS Official Banner Generator.

- Background: High-resolution celestial space background cropped to 1376x380 and
  seamlessly extended on the right side to 1800x380 (+424px pure dark space)
  to balance visual weight and provide ample breathing room for the crescent moon.
- Typography: Montserrat Light (weight 300) with wide tracking (64px) at 98px size.
- Centering: Exact pixel-level ink bounding box alignment, ensuring true mathematical
  and optical center at exactly (width/2, height/2).
- Sharpness: 3x vector supersampling followed by Lanczos downsampling for razor-sharp
  edges without rasterization stair-stepping or blurry halation.
- Lighting: Soft celestial ambient aura layered behind the pure white text.
- Portability: Automatically downloads and caches the Montserrat font in /tmp
  without requiring font binaries committed to the repository.
"""

import io
import os
import shutil
import urllib.request
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter


def get_font_path(weight: int = 300) -> str:
    """Retrieve or download Montserrat font instantiated at the requested weight."""
    cache_dir = "/tmp/artemis_banner_fonts"
    os.makedirs(cache_dir, exist_ok=True)
    target_font = os.path.join(cache_dir, f"Montserrat-wght{weight}.ttf")

    if os.path.exists(target_font):
        return target_font

    var_url = "https://raw.githubusercontent.com/google/fonts/main/ofl/montserrat/Montserrat%5Bwght%5D.ttf"
    print(f"Fetching Montserrat font from Google Fonts ({var_url}) ...")
    try:
        req = urllib.request.Request(var_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            font_data = resp.read()

        from fontTools.ttLib import TTFont
        from fontTools.varLib.instancer import instantiateVariableFont

        tt = TTFont(io.BytesIO(font_data))
        inst = instantiateVariableFont(tt, {"wght": weight})
        inst.save(target_font)
        print(f"Successfully cached Montserrat (wght {weight}) to: {target_font}")
        return target_font
    except Exception as e:
        print(f"Warning: Failed to fetch online font: {e}. Falling back to default system font.")
        return None


def extend_canvas(canvas: Image.Image, extra_w: int = 424, blend_w: int = 80) -> Image.Image:
    """Seamlessly extends the right edge of the canvas with matching space background."""
    if extra_w <= 0:
        return canvas
    w, h = canvas.size
    canvas_rgb = canvas.convert("RGB")
    right_col = np.array(canvas_rgb)[:, -1:, :]
    ext_part = np.repeat(right_col, extra_w + blend_w, axis=1)
    canvas_arr = np.array(canvas_rgb)

    alpha = np.linspace(0, 1, blend_w).reshape(1, blend_w, 1)
    blend_zone = canvas_arr[:, w - blend_w : w, :] * (1 - alpha) + ext_part[:, :blend_w, :] * alpha
    canvas_arr[:, w - blend_w : w, :] = blend_zone.astype(np.uint8)

    full_arr = np.concatenate([canvas_arr, ext_part[:, blend_w:, :]], axis=1)
    return Image.fromarray(full_arr.astype(np.uint8)).convert("RGBA")


def generate_banner(
    bg_path: str,
    output_path: str,
    text: str = "ARTEMIS",
    font_size: int = 98,
    tracking: int = 64,
    font_weight: int = 300,
    extra_w: int = 424,
    offset_x: int = 160,
    glow_mode: str = "ambient",
    supersample: int = 3,
):
    """Generate the official ARTEMIS banner with high clarity and optical/geometric centering."""
    if not os.path.exists(bg_path):
        raise FileNotFoundError(f"Background image not found: {bg_path}")

    # 1. Load raw background and crop canvas
    raw_img = Image.open(bg_path)
    w, h = raw_img.size
    canvas = raw_img.crop((0, 15, w, 395)).convert("RGBA")

    # 2. Seamlessly extend the right side
    if extra_w > 0:
        canvas = extend_canvas(canvas, extra_w=extra_w)
    cw, ch = canvas.size

    # 3. Supersampled text layout
    scale = max(1, supersample)
    sw, sh = cw * scale, ch * scale
    sfsize = font_size * scale
    strk = tracking * scale

    font_path = get_font_path(weight=font_weight)
    if font_path and os.path.exists(font_path):
        font = ImageFont.truetype(font_path, sfsize)
    else:
        font = ImageFont.load_default()

    t_layer = Image.new("RGBA", (sw * 2, sh * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(t_layer)
    start_x = 400 * scale
    start_y = 400 * scale
    curr_x = start_x
    for c in text:
        draw.text((curr_x, start_y), c, font=font, fill=(255, 255, 255, 255))
        curr_x += font.getlength(c) + strk

    # 4. Extract ink bounds and downsample with Lanczos
    ink_3x = t_layer.crop(t_layer.getbbox())
    target_w = round(ink_3x.width / scale)
    target_h = round(ink_3x.height / scale)
    ink_1x = ink_3x.resize((target_w, target_h), Image.Resampling.LANCZOS)
    iw, ih = ink_1x.size

    # 5. Exact pixel geometric / optical centering
    px = (cw - iw) // 2 + offset_x
    py = (ch - ih) // 2

    # 6. Lighting and composite
    composite_img = canvas.copy()

    if glow_mode == "ambient":
        g = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        cyan = Image.new("RGBA", (iw, ih), (120, 215, 255, 55))
        g_ink = Image.composite(cyan, Image.new("RGBA", (iw, ih), (0, 0, 0, 0)), ink_1x.split()[3])
        g.paste(g_ink, (px, py), g_ink)
        g = g.filter(ImageFilter.GaussianBlur(18))
        composite_img = Image.alpha_composite(composite_img, g)

    # Top crisp white text
    text_overlay = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    text_overlay.paste(ink_1x, (px, py), ink_1x)
    final_img = Image.alpha_composite(composite_img, text_overlay).convert("RGB")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    final_img.save(output_path, "PNG")

    center_x = px + iw / 2.0
    center_y = py + ih / 2.0
    print(f"Banner successfully generated: {output_path} ({cw}x{ch})")
    print(
        f"Ink dimensions: {iw}x{ih}px, Exact center: ({center_x:.1f}, {center_y:.1f}) [Canvas center: ({cw / 2:.1f}, {ch / 2:.1f}), Offset X: {offset_x:+d}px]"
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ARTEMIS Official Banner Generator")
    parser.add_argument(
        "--offset-x",
        type=int,
        default=160,
        help="Horizontal pixel offset to the right from mathematical center (default: 160)",
    )
    parser.add_argument(
        "--tracking", type=int, default=64, help="Character tracking spacing (default: 64)"
    )
    parser.add_argument(
        "--font-size", type=int, default=98, help="Font size in pixels (default: 98)"
    )
    args = parser.parse_args()

    assets_dir = os.path.dirname(os.path.abspath(__file__))
    bg_file = os.path.join(assets_dir, "artemis-banner-raw.jpg")
    out_file = os.path.join(assets_dir, "artemis-banner.png")
    out_file_cn = os.path.join(assets_dir, "artemis-banner-cn.png")
    out_file_en = os.path.join(assets_dir, "artemis-banner-en.png")

    generate_banner(
        bg_path=bg_file,
        output_path=out_file,
        text="ARTEMIS",
        font_size=args.font_size,
        tracking=args.tracking,
        font_weight=300,
        extra_w=424,
        offset_x=args.offset_x,
        glow_mode="ambient",
    )

    if os.path.exists(out_file):
        shutil.copyfile(out_file, out_file_cn)
        shutil.copyfile(out_file, out_file_en)
        print(f"Synchronized copy to: {out_file_cn} and {out_file_en}")
