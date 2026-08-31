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

import os
import subprocess
from pathlib import Path


def create_banner_html(lang="en"):
    html_template = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
  * {
    margin: 0;
    padding: 0;
    box-sizing: border-box;
  }

  body {
    width: 1280px;
    height: 480px;
    background: radial-gradient(circle at 50% 30%, #0A0F26 0%, #050711 75%, #020308 100%);
    font-family: 'Roboto', 'Noto Sans', 'Noto Sans CJK SC', -apple-system, BlinkMacSystemFont, sans-serif;
    color: #FFFFFF;
    position: relative;
    overflow: hidden;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 60px 0 68px;
  }

  /* Deep Cyber Micro-Grid */
  .grid-bg {
    position: absolute;
    inset: 0;
    background-image: 
      linear-gradient(to right, rgba(255, 255, 255, 0.035) 1px, transparent 1px),
      linear-gradient(to bottom, rgba(255, 255, 255, 0.035) 1px, transparent 1px);
    background-size: 32px 32px;
    mask-image: radial-gradient(ellipse 90% 80% at 50% 50%, #000 50%, transparent 100%);
    -webkit-mask-image: radial-gradient(ellipse 90% 80% at 50% 50%, #000 50%, transparent 100%);
    pointer-events: none;
    z-index: 1;
  }

  /* Bold Ambient Aurora Glows */
  .glow-cyan {
    position: absolute;
    top: -180px;
    left: -100px;
    width: 720px;
    height: 720px;
    background: radial-gradient(circle, rgba(0, 242, 254, 0.35) 0%, rgba(56, 189, 248, 0.18) 35%, rgba(79, 70, 229, 0.08) 55%, transparent 70%);
    filter: blur(85px);
    pointer-events: none;
    z-index: 2;
  }

  .glow-violet {
    position: absolute;
    bottom: -180px;
    left: 260px;
    width: 640px;
    height: 640px;
    background: radial-gradient(circle, rgba(168, 85, 247, 0.28) 0%, rgba(217, 70, 239, 0.12) 45%, transparent 70%);
    filter: blur(90px);
    pointer-events: none;
    z-index: 2;
  }

  .glow-emerald {
    position: absolute;
    bottom: -150px;
    right: -20px;
    width: 660px;
    height: 660px;
    background: radial-gradient(circle, rgba(16, 185, 129, 0.3) 0%, rgba(6, 182, 212, 0.14) 45%, transparent 70%);
    filter: blur(90px);
    pointer-events: none;
    z-index: 2;
  }

  .glow-top-right {
    position: absolute;
    top: -120px;
    right: 200px;
    width: 520px;
    height: 520px;
    background: radial-gradient(circle, rgba(59, 130, 246, 0.24) 0%, transparent 65%);
    filter: blur(80px);
    pointer-events: none;
    z-index: 2;
  }

  /* Left Column: Bold, Clean & Crisp */
  .left-col {
    position: relative;
    z-index: 10;
    width: 510px;
    display: flex;
    flex-direction: column;
    gap: 22px;
  }

  .brand-title {
    font-size: 82px;
    font-weight: 900;
    letter-spacing: -0.04em;
    line-height: 0.92;
    background: linear-gradient(135deg, #FFFFFF 0%, #F1F5F9 45%, #7DD3FC 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    filter: drop-shadow(0 0 35px rgba(56, 189, 248, 0.45));
  }

  /* Tagline */
  .brand-tagline {
    font-size: 24px;
    font-weight: 700;
    line-height: 1.35;
    color: #F8FAFC;
    letter-spacing: -0.015em;
  }

  .brand-tagline span {
    background: linear-gradient(135deg, #38BDF8 0%, #34D399 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
  }

  /* Ultra-Clean Prompt / Test Directive Box */
  .prompt-box {
    background: rgba(12, 18, 38, 0.88);
    border: 1px solid rgba(56, 189, 248, 0.35);
    border-radius: 14px;
    padding: 16px 18px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    backdrop-filter: blur(24px);
    box-shadow: 0 12px 35px rgba(0, 0, 0, 0.55), inset 0 1px 0 rgba(255, 255, 255, 0.15);
  }

  .prompt-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .prompt-label {
    font-size: 11px;
    font-weight: 800;
    font-family: 'JetBrains Mono', 'DejaVu Sans Mono', monospace;
    color: #38BDF8;
    letter-spacing: 0.08em;
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .prompt-status {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 10px;
    font-family: 'JetBrains Mono', 'DejaVu Sans Mono', monospace;
    color: #34D399;
    font-weight: 800;
  }

  .status-pulse {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: #34D399;
    box-shadow: 0 0 10px #34D399;
  }

  .prompt-text {
    font-size: 13.5px;
    line-height: 1.45;
    font-family: 'JetBrains Mono', 'DejaVu Sans Mono', monospace;
    color: #E2E8F0;
    font-weight: 500;
  }

  .prompt-text span.prompt-prefix {
    color: #38BDF8;
    font-weight: 800;
  }

  .prompt-text span.prompt-target {
    color: #FCD34D;
    font-weight: 700;
  }

  /* Right Visual Stage: Host Agent Console + Real Phone */
  .right-stage {
    position: relative;
    z-index: 10;
    width: 610px;
    height: 440px;
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 20px;
  }

  /* Lean Host Test Runner Console */
  .agent-console {
    position: relative;
    width: 280px;
    background: rgba(11, 16, 33, 0.94);
    border: 1px solid rgba(255, 255, 255, 0.14);
    border-radius: 16px;
    padding: 16px 18px;
    backdrop-filter: blur(25px);
    box-shadow: 0 20px 50px rgba(0, 0, 0, 0.7), inset 0 1px 0 rgba(255, 255, 255, 0.18);
    display: flex;
    flex-direction: column;
    gap: 12px;
    z-index: 15;
  }

  .console-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    padding-bottom: 10px;
  }

  .console-title-group {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .console-dot {
    width: 8px;
    height: 8px;
    background: #00F2FE;
    border-radius: 50%;
    box-shadow: 0 0 10px #00F2FE;
  }

  .console-title {
    font-size: 12px;
    font-weight: 800;
    font-family: 'JetBrains Mono', 'DejaVu Sans Mono', monospace;
    letter-spacing: 0.05em;
    color: #F8FAFC;
  }

  .console-badge {
    font-size: 9.5px;
    font-family: 'JetBrains Mono', 'DejaVu Sans Mono', monospace;
    font-weight: 800;
    padding: 2px 8px;
    background: rgba(52, 211, 153, 0.16);
    color: #34D399;
    border-radius: 4px;
    border: 1px solid rgba(52, 211, 153, 0.35);
  }

  /* Minimal 3-Step Execution List */
  .step-list {
    display: flex;
    flex-direction: column;
    gap: 9px;
  }

  .step-item {
    display: flex;
    align-items: center;
    gap: 9px;
    font-size: 11.5px;
    font-family: 'JetBrains Mono', 'DejaVu Sans Mono', monospace;
  }

  .step-icon-done {
    color: #34D399;
    font-weight: 800;
    font-size: 13px;
  }

  .step-text-done {
    color: #94A3B8;
  }

  /* Active Assertion Highlight Card */
  .step-active-card {
    background: linear-gradient(135deg, rgba(56, 189, 248, 0.18), rgba(16, 185, 129, 0.12));
    border: 1px solid rgba(56, 189, 248, 0.5);
    border-radius: 10px;
    padding: 10px 12px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    box-shadow: 0 4px 18px rgba(56, 189, 248, 0.22);
  }

  .step-active-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .step-active-title {
    font-size: 11.5px;
    font-weight: 800;
    color: #BAE6FD;
    font-family: 'JetBrains Mono', 'DejaVu Sans Mono', monospace;
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .step-active-tag {
    font-size: 9px;
    font-family: 'JetBrains Mono', 'DejaVu Sans Mono', monospace;
    color: #34D399;
    font-weight: 800;
    background: rgba(52, 211, 153, 0.2);
    padding: 2px 6px;
    border-radius: 3px;
  }

  .assertion-result {
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-family: 'JetBrains Mono', 'DejaVu Sans Mono', monospace;
    font-size: 10.5px;
    padding-top: 2px;
  }

  .assert-label {
    color: #CBD5E1;
  }

  .assert-val {
    color: #34D399;
    font-weight: 800;
  }

  /* Connected Mobile Phone Mockup */
  .phone-frame {
    position: relative;
    width: 220px;
    height: 386px;
    background: #0B0F1D;
    border-radius: 36px;
    padding: 8px;
    box-shadow: 
      0 25px 65px -10px rgba(0, 0, 0, 0.92),
      0 0 45px rgba(56, 189, 248, 0.32),
      inset 0 0 0 1.5px rgba(255, 255, 255, 0.2),
      inset 0 1px 3px rgba(255, 255, 255, 0.45);
    z-index: 20;
    flex-shrink: 0;
  }

  .phone-screen {
    width: 100%;
    height: 100%;
    background: linear-gradient(180deg, #0E172E 0%, #070B18 100%);
    border-radius: 28px;
    position: relative;
    overflow: hidden;
    border: 1px solid rgba(255, 255, 255, 0.08);
    display: flex;
    flex-direction: column;
    padding: 14px 12px;
  }

  /* Dynamic Island */
  .phone-island {
    position: absolute;
    top: 7px;
    left: 50%;
    transform: translateX(-50%);
    width: 58px;
    height: 13px;
    background: #000;
    border-radius: 20px;
    z-index: 30;
  }

  .screen-header {
    margin-top: 18px;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  .screen-title {
    font-size: 11px;
    font-weight: 800;
    color: #F8FAFC;
  }

  .device-tag {
    font-size: 8px;
    font-family: 'JetBrains Mono', 'DejaVu Sans Mono', monospace;
    font-weight: 800;
    padding: 2px 6px;
    background: rgba(52, 211, 153, 0.16);
    color: #34D399;
    border-radius: 4px;
    border: 1px solid rgba(52, 211, 153, 0.35);
  }

  /* Music Playing Card */
  .mock-item-card {
    margin-top: 14px;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 12px;
    padding: 10px 10px;
    display: flex;
    gap: 9px;
    align-items: center;
  }

  .item-thumb {
    width: 32px;
    height: 32px;
    border-radius: 8px;
    background: linear-gradient(135deg, #1E293B, #0284C7);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 15px;
  }

  .item-info {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .item-title {
    font-size: 10px;
    font-weight: 800;
    color: #F1F5F9;
  }

  .item-sub {
    font-size: 8px;
    color: #94A3B8;
  }

  .item-price {
    font-size: 9px;
    font-weight: 800;
    color: #38BDF8;
    font-family: 'JetBrains Mono', 'DejaVu Sans Mono', monospace;
  }

  /* Action Target Button with Vision Grounding Box */
  .action-target-wrapper {
    position: relative;
    margin-top: auto;
    margin-bottom: 8px;
  }

  .mock-btn {
    width: 100%;
    height: 38px;
    background: linear-gradient(135deg, #0284C7, #2563EB);
    border-radius: 10px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 11px;
    font-weight: 800;
    color: #FFFFFF;
    box-shadow: 0 4px 16px rgba(2, 132, 199, 0.5);
    border: 1px solid rgba(255, 255, 255, 0.25);
  }

  /* Grounding Box */
  .grounding-box {
    position: absolute;
    inset: -4px;
    border: 1.5px solid #34D399;
    border-radius: 13px;
    background: rgba(52, 211, 153, 0.14);
    box-shadow: 0 0 16px rgba(52, 211, 153, 0.45);
    pointer-events: none;
  }

  .grounding-tag {
    position: absolute;
    top: -9px;
    left: 8px;
    background: #34D399;
    color: #022C22;
    font-family: 'JetBrains Mono', 'DejaVu Sans Mono', monospace;
    font-size: 7.5px;
    font-weight: 900;
    padding: 1px 5px;
    border-radius: 3px;
    letter-spacing: 0.02em;
  }

  /* Tap Ripple */
  .tap-point {
    position: absolute;
    top: 50%;
    right: 14px;
    transform: translateY(-50%);
    width: 22px;
    height: 22px;
    pointer-events: none;
  }

  .tap-ripple {
    position: absolute;
    inset: 0;
    border: 2px solid #FCD34D;
    border-radius: 50%;
    opacity: 0.9;
  }

  .tap-dot {
    position: absolute;
    top: 7px;
    left: 7px;
    width: 8px;
    height: 8px;
    background: #FCD34D;
    border-radius: 50%;
    box-shadow: 0 0 10px #FCD34D;
  }
</style>
</head>
<body>

  <!-- Background Layer -->
  <div class="grid-bg"></div>
  <div class="glow-cyan"></div>
  <div class="glow-violet"></div>
  <div class="glow-emerald"></div>
  <div class="glow-top-right"></div>

  <!-- Left Content Column -->
  <div class="left-col">
    <h1 class="brand-title">ARTEMIS</h1>

    <div class="brand-tagline">
      __TAGLINE__
    </div>

    <!-- Ultra-Clean Test Directive Box -->
    <div class="prompt-box">
      <div class="prompt-header">
        <span class="prompt-label">__PROMPT_LABEL__</span>
        <div class="prompt-status">
          <div class="status-pulse"></div>
          <span>__PROMPT_STATUS__</span>
        </div>
      </div>
      <div class="prompt-text">
        <span class="prompt-prefix">&gt;</span> __PROMPT_TEXT__
      </div>
    </div>
  </div>

  <!-- Right Visual Stage: Host Test Runner Console + Real Connected Phone -->
  <div class="right-stage">
    
    <!-- Lean Host Test Runner Console -->
    <div class="agent-console">
      <div class="console-header">
        <div class="console-title-group">
          <div class="console-dot"></div>
          <span class="console-title">__CONSOLE_TITLE__</span>
        </div>
        <span class="console-badge">__CONSOLE_BADGE__</span>
      </div>

      <div class="step-list">
        <div class="step-item">
          <span class="step-icon-done">✓</span>
          <span class="step-text-done">__STEP_1__</span>
        </div>
        <div class="step-item">
          <span class="step-icon-done">✓</span>
          <span class="step-text-done">__STEP_2__</span>
        </div>

        <!-- Active Assertion Step -->
        <div class="step-active-card">
          <div class="step-active-header">
            <span class="step-active-title">__STEP_3_TITLE__</span>
            <span class="step-active-tag">__STEP_3_TAG__</span>
          </div>
          <div class="assertion-result">
            <span class="assert-label">CPU &lt; 5.0%</span>
            <span class="assert-val">4.2% PASS</span>
          </div>
        </div>
      </div>
    </div>

    <!-- Real Connected Phone Displaying Actual App UI -->
    <div class="phone-frame">
      <div class="phone-screen">
        <div class="phone-island"></div>

        <div class="screen-header">
          <span class="screen-title">__SCREEN_TITLE__</span>
          <span class="device-tag">● PIXEL</span>
        </div>

        <div class="mock-item-card">
          <div class="item-thumb">🎵</div>
          <div class="item-info">
            <span class="item-title">Coldplay - Yellow</span>
            <span class="item-sub">Pixel Buds Pro</span>
          </div>
          <span class="item-price">▶</span>
        </div>

        <div class="action-target-wrapper">
          <!-- AI Grounding Box on Real Mobile Target -->
          <div class="grounding-box">
            <div class="grounding-tag">🎯 GROUNDED</div>
          </div>
          <div class="mock-btn">
            __MOCK_BTN__
          </div>
          <!-- Tap Ripple -->
          <div class="tap-point">
            <div class="tap-ripple"></div>
            <div class="tap-dot"></div>
          </div>
        </div>
      </div>
    </div>

  </div>

</body>
</html>
"""
    if lang == "cn":
        replacements = {
            "__TAGLINE__": "让 AI 助手与测试套件<span>像人一样直接操作真机</span>",
            "__PROMPT_LABEL__": "🎯 测试指令",
            "__PROMPT_STATUS__": "真机运行中",
            "__PROMPT_TEXT__": "连接蓝牙耳机播放音乐，断言 <span class='prompt-target'>CPU 占用 &lt; 5%</span>",
            "__CONSOLE_TITLE__": "真机测试执行引擎",
            "__CONSOLE_BADGE__": "通过",
            "__STEP_1__": "连接蓝牙耳机",
            "__STEP_2__": "打开播放器并播放歌曲",
            "__STEP_3_TITLE__": "▶ 断言性能指标",
            "__STEP_3_TAG__": "实时验证",
            "__SCREEN_TITLE__": "正在播放",
            "__MOCK_BTN__": "音频输出控制",
        }
    else:
        replacements = {
            "__TAGLINE__": "Let AI assistants and test suites <span>use real phones like a human</span>",
            "__PROMPT_LABEL__": "🎯 TEST PROMPT",
            "__PROMPT_STATUS__": "RUNNING",
            "__PROMPT_TEXT__": "Connect Bluetooth earbuds, play song, and assert <span class='prompt-target'>CPU &lt; 5%</span>",
            "__CONSOLE_TITLE__": "Test Runner",
            "__CONSOLE_BADGE__": "PASSED",
            "__STEP_1__": "Connect Bluetooth earbuds",
            "__STEP_2__": "Launch player & play song",
            "__STEP_3_TITLE__": "▶ Assert Performance SLA",
            "__STEP_3_TAG__": "VERIFYING",
            "__SCREEN_TITLE__": "Now Playing",
            "__MOCK_BTN__": "Audio Output",
        }

    for key, val in replacements.items():
        html_template = html_template.replace(key, val)
    return html_template


def render_image(html_str, output_path):
    temp_html = f"/tmp/banner_{os.path.basename(output_path)}.html"
    with open(temp_html, "w", encoding="utf-8") as f:
        f.write(html_str)

    cmd = [
        "google-chrome",
        "--headless",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--window-size=1280,480",
        "--hide-scrollbars",
        f"--screenshot={output_path}",
        f"file://{temp_html}",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if os.path.exists(output_path):
        print(f"Rendered {output_path} ({os.path.getsize(output_path)} bytes)")
    else:
        print(f"Error rendering {output_path}: {res.stderr}")


if __name__ == "__main__":
    assets_dir = Path(__file__).resolve().parent
    os.makedirs(assets_dir, exist_ok=True)

    # Generate English main banner and en banner
    en_html = create_banner_html(lang="en")
    render_image(en_html, str(assets_dir / "artemis-banner.png"))
    render_image(en_html, str(assets_dir / "artemis-banner-en.png"))

    # Generate Chinese banner
    cn_html = create_banner_html(lang="cn")
    render_image(cn_html, str(assets_dir / "artemis-banner-cn.png"))
