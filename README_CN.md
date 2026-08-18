<p align="center">
  <img src="./docs/assets/artemis-banner.png" alt="ARTEMIS Banner" width="100%" />
</p>

<p align="center">
  <strong>ARTEMIS: 全自动操控手机的 AI 个人助理</strong>
</p>

<p align="center">
  <em>⚡ 让 Cursor / Claude Code 接管真机 • 跨 App 复杂自动化 • 零脚本维护 UI 测试 • Flash 模式单步最快 3-5 秒</em>
</p>

<p align="center">
  <a href="./README.md">English</a> •
  <a href="./README_CN.md"><b>中文文档</b></a> •
  <a href="#quick-start">快速上手</a> •
  <a href="#mcp-setup">MCP 接入 IDE</a> •
  <a href="#benchmarks">基准评测</a> •
  <a href="https://discord.gg/wF2FN4WHGY">Discord 社区</a>
</p>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.12+-3776AB.svg?logo=python&logoColor=white" alt="Python 3.12+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-blue.svg" alt="License: Apache-2.0"></a>
  <a href="https://modelcontextprotocol.io/"><img src="https://img.shields.io/badge/MCP-Native%20Server-8A2BE2.svg" alt="MCP Native"></a>
  <a href="https://ai.google.dev/"><img src="https://img.shields.io/badge/Multimodal-Gemini%20%7C%20Claude%20%7C%20GPT--4o%20%7C%20Qwen--VL-4285F4.svg" alt="Multi-Model"></a>
  <a href="https://github.com/google-research/android_world"><img src="https://img.shields.io/badge/AndroidWorld-99%25%2B%20SOTA-success.svg" alt="AndroidWorld SOTA"></a>
</p>

<!-- 演示效果图 -->
<p align="center">
  <img src="./docs/assets/demo.gif" alt="Artemis 演示效果" width="88%" />
  <br>
  <em><b>实机演示</b>：在 Google Maps 中设置驾车路线并计算总耗时，随后打开 YouTube 播放 Coldplay 的歌曲。</em>
</p>

## ✨ 核心亮点

* ⚡ **极致响应速度**：首创**全链路乐观异步流水线 (Optimistic Async Pipeline)**，主干执行流与后台重度推理完全解耦；
* 🔋 **超长程稳定运行**：**Pro 模式**可运行超 10 小时，监控任务甚至可以全天候运行；
* 🔌 **原生 MCP (Model Context Protocol) 赋能**：让 **Cursor、Claude Code、OpenClaw、Windsurf** 直接长出“操作真机的双手”，在聊天窗口一句话完成真机测试与 Bug 复现；
* 💾 **可视化控制台与持久化调试**：提供 Web UI 实时投屏、操作回放与任务持久化管理，支持通过自然语言生成测试用例，并对历史执行进行高保真可视化审查；
* 🏆 **业界顶尖 SOTA**：在 Google Research **AndroidWorld** 基准评测（100+ 复杂长程任务）中取得 **99%+ 任务完成率**。

<a id="quick-start"></a>
<a id="快速上手"></a>
## ⚡ 快速上手

确保电脑已连接 Android 实体机（已开启 **USB 调试**）或 Android 模拟器。

```bash
# 1. 克隆代码仓库并进入目录
git clone https://github.com/google/artemis.git && cd artemis

# 2. 一键启动（自动检测并安装 ADB、scrcpy、FFmpeg、uv 运行时等全部依赖，并拉起控制台）
# 🍎 macOS & 🐧 Linux
./start.sh

# 🪟 Windows (CMD / PowerShell)
start.bat
```

> 💡 **提示**：启动后将自动在默认浏览器中打开 Web 控制台（`http://localhost:8000`），提供设备连接向导、实时投屏、任务演练与状态回放面板。你也可以通过命令行直接运行：`artemis run "打开系统设置，找到电池选项并告诉我当前电量" --profile flash`。

<a id="mcp-setup"></a>
<a id="mcp"></a>
<details>
<summary><b>🔌 接入 Cursor / Claude Code / Windsurf (MCP)（点击展开）</b></summary>

<br>

ARTEMIS 内置原生 **Model Context Protocol (MCP)** 服务。只需将以下配置加入你的 IDE 配置文件中，即可在编写代码时直接驱动真机：

### 1. 生成或查看配置

运行内置命令一键获取当前环境的完整配置 JSON：

```bash
artemis mcp --generate-config cursor
# 或生成所有 IDE 配置：
artemis mcp --generate-config all
```

### 2. 复制配置到 IDE

* **Cursor** (`.cursor/mcp.json` 或 设置 ➔ MCP Servers)：
```json
{
  "mcpServers": {
    "artemis": {
      "command": "python",
      "args": ["-m", "mcp_server"],
      "cwd": "/path/to/artemis",
      "env": {
        "PYTHONUNBUFFERED": "1"
      }
    }
  }
}
```

* **Claude Desktop** (`claude_desktop_config.json`)：
```json
{
  "mcpServers": {
    "artemis": {
      "command": "python",
      "args": ["-m", "mcp_server"],
      "cwd": "/path/to/artemis"
    }
  }
}
```

### 3. 在 IDE 中体验真机协同
在 Cursor / Claude Code 对话框中直接输入：
> 💬 *"请帮我把刚刚修改的代码编译成 APK 并安装到手机上，打开登录页面输入测试账号，验证登录后是否有异常弹窗，并把最终页面截图回传。"*

</details>

<a id="python-sdk"></a>
<details>
<summary><b>🐍 Python SDK 极简集成（点击展开）</b></summary>

<br>

只需几行代码，将移动端自动化智能体无缝嵌入你的 Python 脚本与 CI/CD 流水线中：

```python
import asyncio
from artemis.interfaces.sdk import ArtemisClient


async def main():
    # 初始化客户端（支持 "flash" 极速模式 或 "pro" 深度推理模式）
    client = ArtemisClient(default_profile="flash")

    # 执行自然语言任务
    result = await client.run("打开计算器，计算 (128 * 45) + 330 的结果")

    print(f"执行状态: {result.status}")
    print(f"消耗步数: {result.turns}")
    print(f"Trace ID: {result.trace_id}")


if __name__ == "__main__":
    asyncio.run(main())
```

</details>

## 🕹️ 使用方式

<p align="center">
  <img src="./docs/assets/artemis-ui-showcase.png" alt="Artemis 可视化控制台" width="90%" />
  <br />
  <sub>💡 <b>控制台功能布局</b>：<b>① 顶栏视图切换</b>（主页与工作区） · <b>② 运行模式与录屏回放</b>（Flash/Pro 状态与视频回放） · <b>③ 实时感知推理流</b>（动作分解、点击坐标与结构化总结） · <b>④ 自然语言下发胶囊</b>（自然语言驱动真机） · <b>⑤ 任务队列看板</b>（状态流转与历史回溯）</sub>
</p>

* 🖥️ **Web 可视化控制台 (`artemis ui`)**：集成设备实时投屏与交互面板，支持通过自然语言下发任务，实时观测推理过程、操作轨迹与状态回放；
* 🔌 **MCP 协议集成 (IDE 联动)**：作为标准 MCP 服务器无缝接入 **Cursor、Claude Code、OpenClaw** 等环境，直接在 IDE 中调用移动端真机操作能力；
* 💻 **命令行工具 (`artemis run`)**：支持通过 CLI 直接执行自动化任务、测试用例或 AndroidWorld 基准评测，提供高保真结构化终端输出；
* 🐍 **Python SDK**：作为标准 Python 库集成至现有自动化测试框架或流水线，提供基于 Pydantic 的强类型结构化输出。

## 📊 方案横向对比

| 场景 / 维度 | 传统自动化框架 | 常见屏幕 Agent | **ARTEMIS ☕** |
| :--- | :--- | :--- | :--- |
| **自然语言指令支持** | ❌ 仅支持硬编码脚本，无语义理解 | ⚠️ 响应较慢，单步交互通常需 20-30 秒 | ⚡ **高效执行**：Flash 模式下单步最快约 3-5 秒，交互流畅 |
| **UI 改版适应能力** | ❌ 元素变更易导致脚本执行中断 | ⚠️ 依赖绝对坐标，存在位置偏移风险 | 🎯 **多模态语义定位**：结合文本、图标与层次结构，自适应 UI 变化 |
| **异常弹窗处理** | ❌ 弹窗遮挡易导致定位失败 | ❌ 面对未知弹窗易陷入无效循环 | 🩹 **执行前校验与自愈**：操作前确认目标状态，自动识别并处理干扰弹窗 |
| **动态与视频场景** | ❌ 仅支持静态等待 (sleep)，无动态感知 | ❌ 无法识别视频与动态流媒体内容 | ⏱️ **流媒体分析**：支持视频流分析、倒计时识别与预测性连点 |
| **开发环境集成** | ❌ 独立运行，难以直接与 AI IDE 联动 | ❌ 通常仅提供独立网页，集成成本高 | 🔌 **原生 MCP 支持**：可在 Cursor、Claude Desktop 等环境中直接调用 |

<a id="benchmarks"></a>
<a id="基准评测"></a>
## 🏆 基准评测：AndroidWorld (SOTA 99%+)

在 Google Research 发布的业界基准评测 [AndroidWorld](https://github.com/google-research/android_world)（涵盖 20+ 款常用应用与 100+ 项复杂多步长程任务）中：**Artemis 在全套长程任务评测中展现了高鲁棒性，取得了超过 99% 的任务完成率。**

<p align="center">
  <img src="./docs/assets/androidworld_benchmark_comparison.png" alt="AndroidWorld 评测基准对比" width="85%" />
</p>

## 🚀 ARTEMIS 是如何建构的

* ⚡ **全链路乐观异步 (Optimistic Async)**：主交互循环毫秒级响应下发，记忆修剪与阶段断言全部在后台并发静默运行，告别死等；
* 🛡️ **Safety Net 动作守门**：动作下发前毫秒级双重校验目标，突发弹窗即刻拦截自愈，绝不盲点空气；
* ⏱️ **时效敏感预测性连点 (Speculative Chaining)**：针对视频全屏等瞬态交互，结合历史先验预测坐标并极速连点，彻底解决大模型延迟导致控件消失的问题。

<details>
<summary><b>🔍 点击展开：核心架构设计细节与流水线流程图</b></summary>

<br>

### 1. ⚡ 全链路乐观异步流水线 (Optimistic Asynchronous Pipeline)
* **现有方案与问题**：传统移动端智能体普遍采用**全同步阻塞模型** —— 每一步交互都必须同步串行等待大模型完成历史上下文修剪、阶段性断言检查与长程规划审计。这导致主交互循环极度臃肿，单步延迟动辄 20–40 秒，交互体验迟缓；
* **Artemis 的架构解法**：借鉴数据库的**乐观并发控制 (OCC) 与快照隔离**思想，将主干执行流与重度计算彻底解耦：
  * **高吞吐前台主循环**：前台严格收敛为「感知 → 决策 → 守门 → 执行」极简流水线，单步响应压缩至秒级；
  * **后台并发异步处理**：上下文修剪、里程碑断言校验（Checker）、全局规划审计（Planner）全部在后台并发静默运行，零阻塞前台；
  * **快照隔离与无损回滚**：前台以前瞻视角乐观执行；一旦后台异步断言检测到状态偏离，系统基于快照机制**一键无损回滚 (Rollback)** 并注入纠偏反馈，兼得极低延迟与可靠自愈。

<p align="center">
  <img src="./docs/assets/artemis-architecture-pipeline.png" alt="Artemis 乐观异步流水线架构" width="100%" />
</p>

### 2. 🛡️ 动作守门机制 (Safety Net) 与时效敏感预测性连点 (Speculative Chaining)
* **现有方案与时效困境**：
  * 移动端存在大量**时效敏感型交互**（如视频全屏播放：必须先轻点屏幕唤醒浮层控件，再点击全屏按钮）。
  * 传统智能体在点击屏幕唤醒控件后，重新截屏并等待大模型推理决策耗时数秒；而此时**播放器控件早已自动淡出隐藏**，导致后续点击直接打在视频画面上，陷入“唤醒 → 控件消失 → 误触暂停 → 再次唤醒”的死循环。
* **Artemis 的架构解法**：
  * **预测性链式连点 (Speculative Chaining)**：智能体在识别此类时序依赖后，直接下发复合连击指令（唤醒 → 毫秒级连击全屏），在控件有效窗口期内瞬时完成交互；
  * **保障连点绝对可靠的两大支柱**：
    1. **历史先验位置预测**：基于历史交互轨迹与应用 UI 先验知识，精准预判控件唤醒后的目标坐标；
    2. **Safety Net 毫秒级拦截守门**：在连击动作落下的前一刻，瞬间核验目标控件是否已被成功唤醒并位于预判位置。若唤醒失败或遭遇意外弹窗遮挡，**立刻前置拦截熔断，杜绝盲点误触**。

</details>

## ⚡ 运行模式对比：Flash vs. Pro

| 特性对比 | ⚡ **ARTEMIS Flash** (`--profile flash`) | 🧠 **ARTEMIS Pro** (`--profile pro`) |
| :--- | :--- | :--- |
| **设计定位** | **轻量极速**：面向常规确定性操作 | **深度规划**：面向长流程与复杂自愈 |
| **单步延迟** | **3–5 秒** / 步 | **15–30 秒** / 轮 (包含深度推理与全局校验) |
| **任务时长** | 分钟级短程任务（通常 ≤35 步） | 可稳定运行**超 10 小时**，监控任务可**全天候运行** |
| **适用场景** | 目标明确的常规 UI 操作 | 复杂的跨 App 长流程、故障自愈、持续状态观察 |
| **自愈机制** | 基础局部重试 | **Safety Net 校验** + 弹窗处理 + 崩溃恢复 + 快照回滚 |
| **多媒体分析** | 基础视觉感知 + 高速 OCR | 完整 `scrcpy`/`ffmpeg` 视频流分析 + Logcat 日志采集 |

## 🗺️ 路线图

- [x] **全链路乐观异步流水线**：主流程轻量化，后台并发执行记忆压缩与断言校验。
- [x] **Safety Net 执行前校验**：动作下发前双层核验与预测性链式执行。
- [x] **时间敏感多媒体处理**：集成 `scrcpy` 与 `ffmpeg` 分析全屏视频与音频流。
- [x] **原生 MCP 服务支持**：支持与 Cursor、Claude Desktop 等工具无缝集成。
- [x] **Web 可视化控制台**：提供设备投屏、交互演练与轨迹回溯。
- [x] **AndroidWorld SOTA**：达到 99%+ 的任务完成率。
- [ ] **跨平台扩展**：探索 iOS 与 Web 端的定位与操作能力。
- [ ] **端侧轻量化模型**：支持在设备本地离线运行的轻量级 Edge VLM 模型。
- [ ] **实时语音双工交互**：支持自然语音输入与实时打断控制。

## 🤝 社区与贡献

欢迎通过以下方式参与项目建设：
* ⭐ **Star 本项目**以关注最新进展与更新
* 💬 加入 [Discord 社区](https://discord.gg/wF2FN4WHGY) 参与技术探讨与功能建议
* 🐛 提交 [Issue](https://github.com/google/artemis/issues) 反馈 Bug，欢迎发起 [Pull Request](https://github.com/google/artemis/pulls) 贡献代码

## 📄 开源许可证

本项目基于 [Apache License 2.0](LICENSE) 协议开源。
