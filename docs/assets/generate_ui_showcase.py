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
import os
from pathlib import Path
import subprocess


def generate_showcase(lang="zh", source_image=None):
    assets_dir = Path(__file__).resolve().parent
    if source_image and os.path.exists(source_image):
        image_path = Path(source_image)
    else:
        # Default lookup order
        candidates = [
            assets_dir / "ui_raw.png",
            assets_dir.parent.parent / "image.png",
            assets_dir / "artemis-ui-showcase.png",
        ]
        image_path = next((p for p in candidates if p.exists()), None)

    if lang == "zh":
        output_path = assets_dir / "artemis-ui-showcase.png"
    else:
        output_path = assets_dir / "artemis-ui-showcase-en.png"

    if not image_path or not image_path.exists():
        print(f"Skipping showcase generation: source image not found for {lang}")
        return

    with open(image_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode("utf-8")

    if lang == "zh":
        tag1_title, tag1_desc = "顶栏视图切换", "主页 / 工作区"
        tag2_title, tag2_desc = "运行模式与录屏回放", "Pro / Flash · 回放"
        tag3_title = "实时推理流与操作轨迹"
        tag4_title, tag4_desc = "自然语言下发胶囊", "一句话驱动真机"
        tag5_title, tag5_desc = "任务队列看板", "状态流转 · 历史回溯"

        card3_title = "③ 实时感知推理流解析"
        card3_tag = "LIVE STREAM"
        row1_title, row1_desc = (
            "动作感知与定位",
            "捕获点击、滑动与定位坐标（如 <code>[[381, 430]]</code>）",
        )
        row2_title, row2_desc = (
            "资源开销统计",
            "记录每步耗时（<code>Worked for 13s</code>）与 Token 消耗",
        )
        row3_title, row3_desc = (
            "挂载 Outputter 结构化输出",
            "任务结束时自动调起 Outputter 进行全轨迹证据核验与提取 (<code>output.md</code> / JSON)",
        )

        tag1_pos = "top: 20px; left: 295px;"
        tag2_pos = "top: 76px; left: 660px;"
        tag3_pos = "top: 68px; left: 24px;"
        tag4_pos = "top: 494px; left: 360px;"
        tag5_pos = "top: 70px; left: 1075px;"
        card3_width = "545px"
    else:
        tag1_title, tag1_desc = "View Switcher", "Home / Workspace"
        tag2_title, tag2_desc = "Model & Replay", "Pro / Flash · Replay"
        tag3_title = "Live Agent Execution Stream"
        tag4_title, tag4_desc = "Prompt Dock", "Autonomous Execution"
        tag5_title, tag5_desc = "Task Queue & Dashboard", "Lifecycle & History"

        card3_title = "③ Live Agent Reasoning & Stream"
        card3_tag = "LIVE STREAM"
        row1_title, row1_desc = (
            "Action Grounding & Perception",
            "Real-time gestures, taps & coordinates (e.g. <code>[[381, 430]]</code>)",
        )
        row2_title, row2_desc = (
            "Latency & Token Metrics",
            "Per-step duration tracking (<code>Worked for 13s</code>) & token cost",
        )
        row3_title, row3_desc = (
            "Mounted Outputter Synthesis",
            "Post-execution trace verification & structured data extraction into <code>output.md</code> / JSON",
        )

        tag1_pos = "top: 20px; left: 295px;"
        tag2_pos = "top: 76px; left: 660px;"
        tag3_pos = "top: 68px; left: 24px;"
        tag4_pos = "top: 494px; left: 350px;"
        tag5_pos = "top: 70px; left: 1075px;"
        card3_width = "550px"

    html_content = f"""<!DOCTYPE html>
<html lang="{"zh-CN" if lang == "zh" else "en"}">
<head>
<meta charset="UTF-8">
<style>
  @import url('https://fonts.googleapis.com/css2?family=Google+Sans:ital,wght@0,400;0,500;0,600;0,700;0,800;0,900;1,400;1,700&family=Google+Sans+Display:wght@400;500;600;700;800;900&family=Google+Sans+Mono:wght@400;500;600;700;800&family=Roboto+Mono:wght@400;500;600;700&family=Noto+Sans+SC:wght@400;500;600;700;800;900&display=swap');

  * {{
    margin: 0;
    padding: 0;
    box-sizing: border-box;
  }}

  body {{
    width: 1545px;
    height: 618px;
    background-color: transparent;
    font-family: 'Google Sans', 'Noto Sans SC', -apple-system, BlinkMacSystemFont, 'Roboto', sans-serif;
    position: relative;
    overflow: hidden;
  }}

  .canvas-container {{
    position: relative;
    width: 1545px;
    height: 618px;
    overflow: hidden;
  }}

  .bg-screenshot {{
    position: absolute;
    top: 0;
    left: 0;
    width: 1545px;
    height: 618px;
    display: block;
  }}

  /* High-Precision Grounding Bounding Boxes */
  .grounding-box {{
    position: absolute;
    box-sizing: border-box;
    pointer-events: none;
  }}

  /* Corner reticles / brackets for rectangular boxes */
  .corner-tl, .corner-tr, .corner-bl, .corner-br {{
    position: absolute;
    width: 10px;
    height: 10px;
  }}
  .corner-tl {{ top: -2px; left: -2px; border-top: 3.5px solid; border-left: 3.5px solid; border-top-left-radius: 5px; }}
  .corner-tr {{ top: -2px; right: -2px; border-top: 3.5px solid; border-right: 3.5px solid; border-top-right-radius: 5px; }}
  .corner-bl {{ bottom: -2px; left: -2px; border-bottom: 3.5px solid; border-left: 3.5px solid; border-bottom-left-radius: 5px; }}
  .corner-br {{ bottom: -2px; right: -2px; border-bottom: 3.5px solid; border-right: 3.5px solid; border-bottom-right-radius: 5px; }}

  /* Region 1: Nav Switcher (Top Left Pill Capsule) */
  .box-nav {{
    top: 15px;
    left: 14px;
    width: 266px;
    height: 50px;
    border: 2.5px solid #2563EB;
    border-radius: 25px;
    background: rgba(37, 99, 235, 0.05);
    box-shadow: 0 0 16px rgba(37, 99, 235, 0.35), inset 0 0 10px rgba(37, 99, 235, 0.1);
  }}

  /* Region 2: Model & Replay (Top Center-Right Pills) */
  .box-model {{
    top: 15px;
    left: 752px;
    width: 252px;
    height: 50px;
    border: 2.5px solid #7C3AED;
    border-radius: 25px;
    background: rgba(124, 58, 237, 0.05);
    box-shadow: 0 0 16px rgba(124, 58, 237, 0.35), inset 0 0 10px rgba(124, 58, 237, 0.1);
  }}

  /* Region 3: Trajectory & Steps (Left Main Area) */
  .box-stream {{
    top: 86px;
    left: 14px;
    width: 390px;
    height: 446px;
    border: 2.5px solid #059669;
    border-radius: 12px;
    background: rgba(5, 150, 105, 0.03);
    box-shadow: 0 0 20px rgba(5, 150, 105, 0.25), inset 0 0 12px rgba(5, 150, 105, 0.08);
  }}

  /* Region 4: Command Capsule (Bottom Center) */
  .box-command {{
    top: 546px;
    left: 432px;
    width: 165px;
    height: 60px;
    border: 2.5px solid #DB2777;
    border-radius: 30px;
    background: rgba(219, 39, 119, 0.05);
    box-shadow: 0 0 20px rgba(219, 39, 119, 0.35), inset 0 0 10px rgba(219, 39, 119, 0.1);
  }}

  /* Region 5: Task Queue & History (Right Sidebar Panel) */
  .box-queue {{
    top: 10px;
    left: 1028px;
    width: 508px;
    height: 598px;
    border: 2.5px solid #D97706;
    border-radius: 10px;
    background: rgba(217, 119, 6, 0.03);
    box-shadow: 0 0 22px rgba(217, 119, 6, 0.25), inset 0 0 14px rgba(217, 119, 6, 0.08);
  }}

  /* Large High-Contrast Pill Callout Tags */
  .pill-tag {{
    position: absolute;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 7px 15px 7px 12px;
    border-radius: 999px;
    box-shadow: 0 6px 20px rgba(0, 0, 0, 0.4), 0 2px 5px rgba(0, 0, 0, 0.25);
    z-index: 50;
    pointer-events: none;
    backdrop-filter: blur(10px);
  }}

  .badge-num {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    border-radius: 50%;
    background: #FFFFFF;
    font-size: 13.5px;
    font-weight: 900;
    line-height: 1;
    flex-shrink: 0;
  }}

  .badge-title {{
    font-size: 15px;
    font-weight: 800;
    letter-spacing: 0.2px;
    color: #FFFFFF;
    white-space: nowrap;
  }}

  .badge-desc {{
    font-size: 13px;
    font-weight: 600;
    opacity: 0.95;
    white-space: nowrap;
    padding-left: 7px;
    border-left: 1.5px solid rgba(255, 255, 255, 0.4);
  }}

  /* Theme color stylings */
  .tag-blue {{
    background: #1D4ED8;
    border: 1.8px solid #93C5FD;
  }}
  .tag-blue .badge-num {{ color: #1D4ED8; }}
  .tag-blue .badge-desc {{ color: #DBEAFE; }}

  .tag-purple {{
    background: #6D28D9;
    border: 1.8px solid #C4B5FD;
  }}
  .tag-purple .badge-num {{ color: #6D28D9; }}
  .tag-purple .badge-desc {{ color: #EDE9FE; }}

  .tag-green {{
    background: #047857;
    border: 1.8px solid #6EE7B7;
  }}
  .tag-green .badge-num {{ color: #047857; }}
  .tag-green .badge-desc {{ color: #D1FAE5; }}

  .tag-pink {{
    background: #BE185D;
    border: 1.8px solid #F9A8D4;
  }}
  .tag-pink .badge-num {{ color: #BE185D; }}
  .tag-pink .badge-desc {{ color: #FCE7F3; }}

  .tag-amber {{
    background: #B45309;
    border: 1.8px solid #FCD34D;
  }}
  .tag-amber .badge-num {{ color: #B45309; }}
  .tag-amber .badge-desc {{ color: #FEF3C7; }}

  /* Informational Floating Guide Panel in Central Empty Area */
  .center-features-panel {{
    position: absolute;
    top: 135px;
    left: 440px;
    width: {card3_width};
    background: rgba(255, 255, 255, 0.96);
    backdrop-filter: blur(16px);
    border: 2px solid rgba(16, 185, 129, 0.45);
    border-radius: 14px;
    box-shadow: 0 14px 36px -4px rgba(0, 0, 0, 0.16), 0 4px 14px rgba(0, 0, 0, 0.08);
    padding: 18px 20px;
    display: flex;
    flex-direction: column;
    gap: 13px;
    z-index: 40;
  }}

  .panel-header {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding-bottom: 10px;
    border-bottom: 1.5px solid rgba(0, 0, 0, 0.08);
  }}

  .panel-title-group {{
    display: flex;
    align-items: center;
    gap: 9px;
  }}

  .panel-icon {{
    display: flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    border-radius: 7px;
    background: rgba(16, 185, 129, 0.18);
    color: #047857;
    font-size: 16px;
  }}

  .panel-title {{
    font-size: 15.5px;
    font-weight: 900;
    color: #0F172A;
    letter-spacing: -0.2px;
  }}

  .panel-tag {{
    font-size: 11px;
    font-weight: 800;
    color: #047857;
    background: rgba(16, 185, 129, 0.15);
    padding: 3px 9px;
    border-radius: 999px;
    letter-spacing: 0.5px;
  }}

  .panel-items {{
    display: flex;
    flex-direction: column;
    gap: 11px;
  }}

  .panel-row {{
    display: flex;
    align-items: flex-start;
    gap: 9px;
    font-size: 13.5px;
    line-height: 1.45;
    color: #1E293B;
  }}

  .row-dot {{
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: #059669;
    margin-top: 6px;
    flex-shrink: 0;
  }}

  .row-strong {{
    font-weight: 800;
    color: #0F172A;
  }}

  .panel-row code {{
    background: #F1F5F9;
    border: 1px solid #CBD5E1;
    padding: 1px 6px;
    border-radius: 4px;
    font-family: 'Google Sans Mono', 'Roboto Mono', monospace;
    font-size: 12.5px;
    font-weight: 700;
    color: #0F172A;
    white-space: nowrap;
  }}

  /* Leader connection line from Step 3 to center card */
  .leader-line {{
    position: absolute;
    top: 240px;
    left: 404px;
    width: 36px;
    height: 2.5px;
    background: #059669;
    box-shadow: 0 0 6px rgba(5, 150, 105, 0.6);
    z-index: 45;
  }}
</style>
</head>
<body>

  <div class="canvas-container">
    <!-- Original Subject Image -->
    <img src="data:image/png;base64,{img_b64}" class="bg-screenshot" alt="Artemis Console" />

    <!-- 1. Nav Switcher (Pill-shaped bounding box snugly wrapping buttons) -->
    <div class="grounding-box box-nav"></div>
    <div class="pill-tag tag-blue" style="{tag1_pos}">
      <span class="badge-num">1</span>
      <span class="badge-title">{tag1_title}</span>
      <span class="badge-desc">{tag1_desc}</span>
    </div>

    <!-- 2. Model & Replay (Pill-shaped bounding box snugly wrapping buttons) -->
    <div class="grounding-box box-model"></div>
    <div class="pill-tag tag-purple" style="{tag2_pos}">
      <span class="badge-num">2</span>
      <span class="badge-title">{tag2_title}</span>
      <span class="badge-desc">{tag2_desc}</span>
    </div>

    <!-- 3. Trajectory & Steps (Rectangular box with corner brackets) -->
    <div class="grounding-box box-stream" style="border-color: #059669;">
      <div class="corner-tl" style="border-color: #34D399;"></div>
      <div class="corner-tr" style="border-color: #34D399;"></div>
      <div class="corner-bl" style="border-color: #34D399;"></div>
      <div class="corner-br" style="border-color: #34D399;"></div>
    </div>
    <div class="pill-tag tag-green" style="{tag3_pos}">
      <span class="badge-num">3</span>
      <span class="badge-title">{tag3_title}</span>
    </div>

    <!-- Floating details card in empty white space for Area 3 -->
    <div class="leader-line"></div>
    <div class="center-features-panel">
      <div class="panel-header">
        <div class="panel-title-group">
          <div class="panel-icon">⚡</div>
          <span class="panel-title">{card3_title}</span>
        </div>
        <span class="panel-tag">{card3_tag}</span>
      </div>
      <div class="panel-items">
        <div class="panel-row">
          <div class="row-dot"></div>
          <div><span class="row-strong">{row1_title}</span>: {row1_desc}</div>
        </div>
        <div class="panel-row">
          <div class="row-dot"></div>
          <div><span class="row-strong">{row2_title}</span>: {row2_desc}</div>
        </div>
        <div class="panel-row">
          <div class="row-dot"></div>
          <div><span class="row-strong">{row3_title}</span>: {row3_desc}</div>
        </div>
      </div>
    </div>

    <!-- 4. Command Capsule (Pill-shaped bounding box) -->
    <div class="grounding-box box-command"></div>
    <div class="pill-tag tag-pink" style="{tag4_pos}">
      <span class="badge-num">4</span>
      <span class="badge-title">{tag4_title}</span>
      <span class="badge-desc">{tag4_desc}</span>
    </div>

    <!-- 5. Task Queue & History (Right Panel) -->
    <div class="grounding-box box-queue" style="border-color: #D97706;">
      <div class="corner-tl" style="border-color: #FBBF24;"></div>
      <div class="corner-tr" style="border-color: #FBBF24;"></div>
      <div class="corner-bl" style="border-color: #FBBF24;"></div>
      <div class="corner-br" style="border-color: #FBBF24;"></div>
    </div>
    <div class="pill-tag tag-amber" style="{tag5_pos}">
      <span class="badge-num">5</span>
      <span class="badge-title">{tag5_title}</span>
      <span class="badge-desc">{tag5_desc}</span>
    </div>
  </div>

</body>
</html>
"""

    temp_html = f"/tmp/ui_showcase_render_{lang}.html"
    with open(temp_html, "w", encoding="utf-8") as f:
        f.write(html_content)

    chrome_candidates = [
        "google-chrome",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "chromium",
        "chromium-browser",
    ]
    chrome_bin = next(
        (
            c
            for c in chrome_candidates
            if os.path.exists(c)
            or subprocess.run(["which", c], capture_output=True).returncode == 0
        ),
        "google-chrome",
    )

    cmd = [
        chrome_bin,
        "--headless=new",
        "--disable-gpu",
        "--virtual-time-budget=2000",
        "--window-size=1545,618",
        "--device-scale-factor=2",
        "--hide-scrollbars",
        f"--screenshot={output_path}",
        f"file://{temp_html}",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if os.path.exists(output_path):
        print(f"Successfully generated {output_path} ({os.path.getsize(output_path)} bytes)")
    else:
        print(f"Error rendering: {res.stderr}")


if __name__ == "__main__":
    generate_showcase("zh")
    generate_showcase("en")
