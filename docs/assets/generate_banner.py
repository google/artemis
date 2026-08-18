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


def create_banner_html():
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
  @import url('https://fonts.googleapis.com/css2?family=Google+Sans:ital,wght@0,400;0,500;0,600;0,700;0,800;0,900;1,400;1,700&family=Google+Sans+Display:wght@400;500;600;700;800;900&family=Google+Sans+Mono:wght@400;500;600;700;800&family=Roboto+Mono:wght@400;500;600;700&display=swap');

  * {
    margin: 0;
    padding: 0;
    box-sizing: border-box;
  }

  body {
    width: 1280px;
    height: 480px;
    background-color: #060913;
    font-family: 'Google Sans', -apple-system, BlinkMacSystemFont, 'Roboto', sans-serif;
    color: #FFFFFF;
    position: relative;
    overflow: hidden;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 64px 0 72px;
  }

  /* Fine Cyber Grid */
  .grid-bg {
    position: absolute;
    inset: 0;
    background-image: 
      linear-gradient(to right, rgba(255, 255, 255, 0.025) 1px, transparent 1px),
      linear-gradient(to bottom, rgba(255, 255, 255, 0.025) 1px, transparent 1px);
    background-size: 32px 32px;
    mask-image: radial-gradient(ellipse 90% 80% at 50% 50%, #000 45%, transparent 100%);
    -webkit-mask-image: radial-gradient(ellipse 90% 80% at 50% 50%, #000 45%, transparent 100%);
    pointer-events: none;
  }

  /* Refined Ambient Glows */
  .glow-blue {
    position: absolute;
    top: -140px;
    left: -100px;
    width: 640px;
    height: 640px;
    background: radial-gradient(circle, rgba(66, 133, 244, 0.26) 0%, rgba(66, 133, 244, 0.03) 50%, transparent 70%);
    filter: blur(80px);
    pointer-events: none;
  }

  .glow-green {
    position: absolute;
    bottom: -140px;
    right: 40px;
    width: 550px;
    height: 550px;
    background: radial-gradient(circle, rgba(52, 168, 83, 0.18) 0%, rgba(52, 168, 83, 0.02) 50%, transparent 70%);
    filter: blur(80px);
    pointer-events: none;
  }

  .glow-yellow {
    position: absolute;
    top: 60px;
    right: 320px;
    width: 450px;
    height: 450px;
    background: radial-gradient(circle, rgba(251, 188, 5, 0.09) 0%, transparent 65%);
    filter: blur(75px);
    pointer-events: none;
  }

  .glow-red {
    position: absolute;
    top: -100px;
    left: 460px;
    width: 380px;
    height: 380px;
    background: radial-gradient(circle, rgba(234, 67, 53, 0.08) 0%, transparent 60%);
    filter: blur(75px);
    pointer-events: none;
  }

  /* Left Column: Clean, Minimalist & Bold */
  .left-col {
    position: relative;
    z-index: 10;
    max-width: 470px;
    display: flex;
    flex-direction: column;
    gap: 20px;
  }

  /* Title Row */
  .title-row {
    display: flex;
    align-items: center;
    gap: 20px;
  }

  .logo-wrapper {
    position: relative;
    width: 92px;
    height: 92px;
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .logo-halo {
    position: absolute;
    inset: -8px;
    background: radial-gradient(circle, rgba(66, 133, 244, 0.55) 0%, rgba(52, 168, 83, 0.25) 50%, transparent 75%);
    filter: blur(16px);
    border-radius: 50%;
  }

  .logo-icon {
    position: relative;
    width: 92px;
    height: 92px;
    filter: drop-shadow(0 4px 22px rgba(66, 133, 244, 0.6));
  }

  .brand-title {
    font-family: 'Google Sans Display', 'Google Sans', sans-serif;
    font-size: 78px;
    font-weight: 900;
    letter-spacing: -0.025em;
    line-height: 0.95;
    color: #FFFFFF;
    filter: drop-shadow(0 0 35px rgba(66, 133, 244, 0.4));
  }

  /* Clean Single Subtitle (Preserving Android) */
  .brand-tagline {
    font-size: 24px;
    font-weight: 700;
    line-height: 1.35;
    color: #F8FAFC;
    letter-spacing: -0.01em;
  }

  .brand-tagline span {
    background: linear-gradient(135deg, #60A5FA 0%, #34D399 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
  }

  /* Prompt Input Card */
  .prompt-box {
    background: rgba(15, 23, 42, 0.75);
    border: 1px solid rgba(66, 133, 244, 0.22);
    border-radius: 14px;
    padding: 13px 16px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    backdrop-filter: blur(14px);
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.4);
  }

  .prompt-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .prompt-label {
    font-size: 10px;
    font-weight: 700;
    font-family: 'Google Sans Mono', 'Roboto Mono', monospace;
    color: #64748B;
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }

  .prompt-status {
    display: flex;
    align-items: center;
    gap: 5px;
    font-size: 9.5px;
    font-family: 'Google Sans Mono', 'Roboto Mono', monospace;
    color: #34A853;
    font-weight: 600;
  }

  .status-pulse {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: #34A853;
    box-shadow: 0 0 8px #34A853;
  }

  .prompt-text {
    font-size: 13.5px;
    font-family: 'Google Sans Mono', 'Roboto Mono', monospace;
    color: #E2E8F0;
    font-weight: 500;
  }

  .prompt-text span {
    color: #60A5FA;
    font-weight: 700;
  }

  /* Right Visual Stage: Host Agent Driving Connected Phone */
  .right-stage {
    position: relative;
    z-index: 10;
    width: 630px;
    height: 440px;
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 22px;
  }

  /* Agent Progress Console (Host Controller) */
  .agent-console {
    position: relative;
    width: 285px;
    background: rgba(11, 17, 32, 0.92);
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 16px;
    padding: 14px 16px;
    backdrop-filter: blur(20px);
    box-shadow: 0 20px 50px rgba(0, 0, 0, 0.6), inset 0 1px 0 rgba(255, 255, 255, 0.15);
    display: flex;
    flex-direction: column;
    gap: 11px;
    z-index: 15;
  }

  .console-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    padding-bottom: 8px;
  }

  .console-title-group {
    display: flex;
    align-items: center;
    gap: 7px;
  }

  .console-dot {
    width: 7px;
    height: 7px;
    background: #4285F4;
    border-radius: 50%;
    box-shadow: 0 0 8px #4285F4;
  }

  .console-title {
    font-size: 11px;
    font-weight: 800;
    font-family: 'Google Sans Mono', 'Roboto Mono', monospace;
    letter-spacing: 0.06em;
    color: #F8FAFC;
  }

  .console-badge {
    font-size: 9px;
    font-family: 'Google Sans Mono', 'Roboto Mono', monospace;
    font-weight: 700;
    padding: 2px 6px;
    background: rgba(66, 133, 244, 0.15);
    color: #60A5FA;
    border-radius: 4px;
    border: 1px solid rgba(66, 133, 244, 0.3);
  }

  /* Step Execution Timeline */
  .step-list {
    display: flex;
    flex-direction: column;
    gap: 7px;
  }

  .step-item {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 11px;
    font-family: 'Google Sans Mono', 'Roboto Mono', monospace;
  }

  .step-icon-done {
    color: #34A853;
    font-weight: 800;
    font-size: 11.5px;
  }

  .step-text-done {
    color: #94A3B8;
  }

  /* Active Step Highlight Card */
  .step-active-card {
    background: linear-gradient(135deg, rgba(66, 133, 244, 0.14), rgba(52, 168, 83, 0.08));
    border: 1px solid rgba(66, 133, 244, 0.4);
    border-radius: 9px;
    padding: 8px 10px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    box-shadow: 0 4px 15px rgba(66, 133, 244, 0.15);
  }

  .step-active-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .step-active-title {
    font-size: 11px;
    font-weight: 700;
    color: #93C5FD;
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .step-active-time {
    font-size: 9px;
    font-family: 'Google Sans Mono', 'Roboto Mono', monospace;
    color: #34A853;
    font-weight: 700;
    background: rgba(52, 168, 83, 0.15);
    padding: 1px 5px;
    border-radius: 3px;
  }

  .step-active-desc {
    font-size: 9.5px;
    font-family: 'Google Sans Mono', 'Roboto Mono', monospace;
    color: #CBD5E1;
    line-height: 1.4;
  }

  .step-active-desc span {
    color: #FBBC05;
    font-weight: 700;
  }

  .step-pending {
    color: #475569;
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 11px;
    font-family: 'Google Sans Mono', 'Roboto Mono', monospace;
  }

  /* Connected Mobile Phone Mockup */
  .phone-frame {
    position: relative;
    width: 215px;
    height: 380px;
    background: #0B0F19;
    border-radius: 36px;
    padding: 8px;
    box-shadow: 
      0 25px 65px -10px rgba(0, 0, 0, 0.85),
      0 0 45px rgba(66, 133, 244, 0.25),
      inset 0 0 0 1.5px rgba(255, 255, 255, 0.16),
      inset 0 1px 3px rgba(255, 255, 255, 0.4);
    z-index: 20;
    flex-shrink: 0;
  }

  .phone-screen {
    width: 100%;
    height: 100%;
    background: linear-gradient(180deg, #0D1527 0%, #060B16 100%);
    border-radius: 28px;
    position: relative;
    overflow: hidden;
    border: 1px solid rgba(255, 255, 255, 0.08);
    display: flex;
    flex-direction: column;
    padding: 12px 10px;
  }

  /* Dynamic Island */
  .phone-island {
    position: absolute;
    top: 7px;
    left: 50%;
    transform: translateX(-50%);
    width: 56px;
    height: 13px;
    background: #000;
    border-radius: 20px;
    z-index: 30;
  }

  /* Phone App UI Header */
  .screen-header {
    margin-top: 18px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0 2px;
  }

  .screen-title {
    font-size: 10.5px;
    font-weight: 700;
    color: #F8FAFC;
  }

  .device-tag {
    font-size: 7.5px;
    font-family: 'Google Sans Mono', 'Roboto Mono', monospace;
    font-weight: 700;
    padding: 2px 5px;
    background: rgba(52, 168, 83, 0.15);
    color: #34A853;
    border-radius: 4px;
    border: 1px solid rgba(52, 168, 83, 0.35);
  }

  /* Screen Content Cards */
  .mock-item-card {
    margin-top: 10px;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 11px;
    padding: 8px 9px;
    display: flex;
    gap: 8px;
    align-items: center;
  }

  .item-thumb {
    width: 28px;
    height: 28px;
    border-radius: 7px;
    background: linear-gradient(135deg, #1E293B, #334155);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 13px;
  }

  .item-info {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 1.5px;
  }

  .item-title {
    font-size: 9px;
    font-weight: 700;
    color: #F1F5F9;
  }

  .item-sub {
    font-size: 7.5px;
    color: #64748B;
  }

  .item-price {
    font-size: 9px;
    font-weight: 700;
    color: #38BDF8;
    font-family: 'Google Sans Mono', 'Roboto Mono', monospace;
  }

  /* Secondary Mock Card */
  .mock-secondary-card {
    margin-top: 7px;
    background: rgba(255, 255, 255, 0.025);
    border: 1px solid rgba(255, 255, 255, 0.05);
    border-radius: 9px;
    padding: 6px 8px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    opacity: 0.8;
  }

  .secondary-text {
    font-size: 7.5px;
    color: #94A3B8;
  }

  .secondary-val {
    font-size: 7.5px;
    font-weight: 700;
    color: #F1F5F9;
    font-family: 'Google Sans Mono', 'Roboto Mono', monospace;
  }

  /* Action Target Button with Vision Grounding Box */
  .action-target-wrapper {
    position: relative;
    margin-top: auto;
    margin-bottom: 6px;
  }

  .mock-btn {
    width: 100%;
    height: 36px;
    background: linear-gradient(135deg, #4285F4, #2563EB);
    border-radius: 9px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 10px;
    font-weight: 800;
    color: #FFFFFF;
    box-shadow: 0 4px 15px rgba(66, 133, 244, 0.45);
    border: 1px solid rgba(255, 255, 255, 0.2);
  }

  /* AI Grounding Vision Box (Emerald Green) */
  .grounding-box {
    position: absolute;
    inset: -4px;
    border: 1.5px solid #34A853;
    border-radius: 12px;
    background: rgba(52, 168, 83, 0.12);
    box-shadow: 0 0 15px rgba(52, 168, 83, 0.35);
    pointer-events: none;
  }

  .grounding-tag {
    position: absolute;
    top: -9px;
    left: 8px;
    background: #34A853;
    color: #032014;
    font-family: 'Google Sans Mono', 'Roboto Mono', monospace;
    font-size: 7.5px;
    font-weight: 800;
    padding: 1px 5px;
    border-radius: 3px;
    letter-spacing: 0.02em;
  }

  /* Tap Ripple Target (Amber/Yellow Accent) */
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
    border: 2px solid #FBBC05;
    border-radius: 50%;
    opacity: 0.85;
  }

  .tap-dot {
    position: absolute;
    top: 7px;
    left: 7px;
    width: 8px;
    height: 8px;
    background: #FBBC05;
    border-radius: 50%;
    box-shadow: 0 0 10px #FBBC05;
  }
</style>
</head>
<body>

  <!-- Background Layer -->
  <div class="grid-bg"></div>
  <div class="glow-blue"></div>
  <div class="glow-green"></div>
  <div class="glow-yellow"></div>
  <div class="glow-red"></div>

  <!-- Left Content Column (Clean, Focused, Minimalist) -->
  <div class="left-col">
    <div class="title-row">
      <div class="logo-wrapper">
        <div class="logo-halo"></div>
        <svg class="logo-icon" viewBox="0 0 240 240" fill="none" xmlns="http://www.w3.org/2000/svg">
          <defs>
            <radialGradient id="redGlow" cx="50%" cy="50%" r="50%">
              <stop offset="0%" stop-color="#EA4335" stop-opacity="1" />
              <stop offset="40%" stop-color="#EA4335" stop-opacity="0.95" />
              <stop offset="100%" stop-color="#EA4335" stop-opacity="0" />
            </radialGradient>
            <radialGradient id="yellowGlow" cx="50%" cy="50%" r="50%">
              <stop offset="0%" stop-color="#FBBC05" stop-opacity="1" />
              <stop offset="40%" stop-color="#FBBC05" stop-opacity="0.95" />
              <stop offset="100%" stop-color="#FBBC05" stop-opacity="0" />
            </radialGradient>
            <radialGradient id="greenGlow" cx="50%" cy="50%" r="50%">
              <stop offset="0%" stop-color="#34A853" stop-opacity="1" />
              <stop offset="40%" stop-color="#34A853" stop-opacity="0.95" />
              <stop offset="100%" stop-color="#34A853" stop-opacity="0" />
            </radialGradient>
            <mask id="robotMeshMask">
              <rect width="240" height="240" fill="black" />
              <path d="M 90 60 Q 75 35 65 28" stroke="white" stroke-width="10" stroke-linecap="round" />
              <circle cx="62" cy="24" r="10" fill="white" />
              <path d="M 150 60 Q 165 35 175 28" stroke="white" stroke-width="10" stroke-linecap="round" />
              <circle cx="178" cy="24" r="10" fill="white" />
              <rect x="28" y="100" width="16" height="48" rx="8" fill="white" />
              <rect x="196" y="100" width="16" height="48" rx="8" fill="white" />
              <rect x="44" y="58" width="152" height="132" rx="40" fill="white" />
              <rect x="64" y="88" width="112" height="52" rx="22" fill="black" />
              <path d="M 82 118 Q 92 106 102 118" stroke="white" stroke-width="6" stroke-linecap="round" fill="none" />
              <path d="M 138 118 Q 148 106 158 118" stroke="white" stroke-width="6" stroke-linecap="round" fill="none" />
              <path d="M 104 158 Q 120 172 136 158" stroke="black" stroke-width="6" stroke-linecap="round" fill="none" />
            </mask>
          </defs>
          <g mask="url(#robotMeshMask)">
            <rect width="240" height="240" fill="#4285F4" />
            <circle cx="160" cy="50" r="100" fill="url(#redGlow)" />
            <circle cx="60" cy="120" r="95" fill="url(#yellowGlow)" />
            <circle cx="110" cy="200" r="95" fill="url(#greenGlow)" />
          </g>
        </svg>
      </div>

      <h1 class="brand-title">ARTEMIS</h1>
    </div>

    <!-- Clear, Crisp Subtitle -->
    <div class="brand-tagline">
      Control Any Android App with <span>Autonomous AI</span>
    </div>

    <!-- User Prompt Input Example -->
    <div class="prompt-box">
      <div class="prompt-header">
        <span class="prompt-label">Goal / Prompt</span>
        <div class="prompt-status">
          <div class="status-pulse"></div>
          <span>RUNNING ON DEVICE</span>
        </div>
      </div>
      <div class="prompt-text">
        <span>&gt;</span> Order an iced caramel latte with oat milk
      </div>
    </div>
  </div>

  <!-- Right Visual Stage: Host Agent Driving Connected Phone -->
  <div class="right-stage">
    
    <!-- Agent Execution Console (Host Side) -->
    <div class="agent-console">
      <div class="console-header">
        <div class="console-title-group">
          <div class="console-dot"></div>
          <span class="console-title">Agent Execution</span>
        </div>
        <span class="console-badge">STEP 3 / 4</span>
      </div>

      <div class="step-list">
        <div class="step-item">
          <span class="step-icon-done">✓</span>
          <span class="step-text-done">1. Open app & search catalog</span>
        </div>
        <div class="step-item">
          <span class="step-icon-done">✓</span>
          <span class="step-text-done">2. Select item & customize</span>
        </div>

        <!-- Active Step -->
        <div class="step-active-card">
          <div class="step-active-header">
            <span class="step-active-title">▶ 3. Tap "Place Order"</span>
            <span class="step-active-time">140ms</span>
          </div>
          <div class="step-active-desc">
            Vision target grounded at <span>(540, 1680)</span>
          </div>
        </div>

        <div class="step-pending">
          <span>○</span>
          <span>4. Verify receipt & return</span>
        </div>
      </div>
    </div>

    <!-- Real Connected Phone Executing the Tap -->
    <div class="phone-frame">
      <div class="phone-screen">
        <div class="phone-island"></div>

        <div class="screen-header">
          <span class="screen-title">Food & Drink</span>
          <span class="device-tag">● ANDROID</span>
        </div>

        <div class="mock-item-card">
          <div class="item-thumb">☕</div>
          <div class="item-info">
            <span class="item-title">Iced Caramel Latte</span>
            <span class="item-sub">Double Shot · Oat Milk</span>
          </div>
          <span class="item-price">$5.40</span>
        </div>

        <div class="mock-secondary-card">
          <span class="secondary-text">Delivery Address</span>
          <span class="secondary-val">Desk #42</span>
        </div>

        <div class="action-target-wrapper">
          <!-- AI Grounding Box on Target Button -->
          <div class="grounding-box">
            <div class="grounding-tag">🎯 GROUNDED (0.99)</div>
          </div>
          <div class="mock-btn">
            Place Order ($5.40)
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


def render_image(html_str, output_path):
    temp_html = f"/tmp/banner_{os.path.basename(output_path)}.html"
    with open(temp_html, "w", encoding="utf-8") as f:
        f.write(html_str)

    cmd = [
        "google-chrome",
        "--headless=new",
        "--disable-gpu",
        "--virtual-time-budget=2000",
        "--window-size=1280,480",
        "--device-scale-factor=2",
        "--hide-scrollbars",
        f"--screenshot={output_path}",
        f"file://{temp_html}",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if os.path.exists(output_path):
        print(f"Rendered {output_path} ({os.path.getsize(output_path)} bytes)")
    else:
        print(f"Error rendering {output_path}: {res.stderr}")


from pathlib import Path

assets_dir = Path(__file__).resolve().parent
os.makedirs(assets_dir, exist_ok=True)

html_content = create_banner_html()

# Generate main banner
render_image(html_content, str(assets_dir / "artemis-banner.png"))
# Also update en banner for consistency
render_image(html_content, str(assets_dir / "artemis-banner-en.png"))
