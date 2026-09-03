<p align="center">
  <img src="./docs/assets/artemis-banner-cn.png?v=6" alt="ARTEMIS Banner" width="100%" />
</p>

<p align="center">
  <strong>让 AI 助手与测试套件像人一样直接操作真机。</strong>
</p>

<p align="center">
  <a href="./README.md">English</a> •
  <a href="./README_CN.md"><b>中文文档</b></a> •
  <a href="#workflow-showcase">全流程演示</a> •
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
  <img src="./docs/assets/demo.gif" alt="Artemis 演示效果" width="100%" />
  <br>
  <em>实机演示：在 Google Maps 中设置驾车路线并计算总耗时，随后打开 YouTube 播放 Coldplay 的歌曲。</em>
</p>

## 核心亮点

* **跨 App 复杂自动化与 AI 个人助理**：不仅能执行严格的自动化测试用例，更具备接管真机的自主决策与长流程操作能力，一句话处理跨应用复杂业务与任务；
* **零脚本维护自动化测试**：基于“动态优先、坐标兜底”的多模态语义定位，彻底告别传统 XPath / 控件 ID 频繁失效的痛点，无惧 App 改版、分辨率差异与系统升级；
* **IDE 内一键 Bug 复现与 Logcat 诊断**：原生支持 **MCP 协议**，可在 **Antigravity、Claude Code、Windsurf** 中直接用自然语言驱动真机复现缺陷，自动抓取关键帧截图与 **Logcat 崩溃堆栈**，完成研发测试闭环；
* **极速执行吞吐（单步 3–5 秒）**：首创**全链路乐观异步流水线 (Optimistic Async Pipeline)**，解耦重度推理与页面操作，在 Flash 模式下实现丝滑的高频交互与快速回归验证；
* **干扰弹窗自愈与超长程巡检**：独创 **Safety Net** 执行前校验机制，自动识别并清除系统权限弹窗、通知遮挡等异常；Pro 模式支持连续 **10+ 小时** 无人值守稳定性巡检与探索性测试 (Exploratory / Monkey-plus Testing)；
* **业界顶尖 SOTA**：在 Google Research **AndroidWorld** 基准评测（100+ 复杂长程系统与应用交互任务）中取得 **99%+ 任务完成率**。

<a id="workflow-showcase"></a>
<a id="全流程演示"></a>
## Antigravity × ARTEMIS：全流程自主测试演示

通过原生 MCP 协议，**Antigravity** 与 **ARTEMIS** 深度协同——只需一句自然语言指令，即可自动完成从需求理解、测试规划、真机执行到深度报告输出的完整闭环：

<table width="100%">
  <tr>
    <td width="50%" align="center">
      <b>1. 输入测试提示词 (Task Dispatch)</b><br>
      <sub>在 Antigravity 中用自然语言描述测试需求与目标指标</sub><br><br>
      <img src="./docs/assets/workflow-1-prompt.png" width="100%" alt="步骤一：输入测试提示词" />
    </td>
    <td width="50%" align="center">
      <b>2. 生成测试方案 (Test Plan Generation)</b><br>
      <sub>自动拆解任务，生成详细测试步骤与架构图供确认</sub><br><br>
      <img src="./docs/assets/workflow-2-plan.png" width="100%" alt="步骤二：生成测试方案" />
    </td>
  </tr>
  <tr>
    <td width="50%" align="center">
      <b>3. 自主执行测试 (Autonomous Test Execution)</b><br>
      <sub>驱动真机操作、界面导航并实时分析性能指标</sub><br><br>
      <img src="./docs/assets/workflow-3-exec.png" width="100%" alt="步骤三：自主执行测试" />
    </td>
    <td width="50%" align="center">
      <b>4. 交付最终报告 (Comprehensive Final Report)</b><br>
      <sub>生成结构化测试报告，交付性能图表、结论与原始数据</sub><br><br>
      <img src="./docs/assets/workflow-4-report.png" width="100%" alt="步骤四：交付最终报告" />
    </td>
  </tr>
</table>

<a id="quick-start"></a>
<a id="快速上手"></a>
## 快速上手

确保电脑已连接 Android 实体机（已开启 **USB 调试**）或 Android 模拟器。一键启动脚本将会自动完成以下配置：
- **安装系统环境依赖**：自动检测并安装 ADB、scrcpy、FFmpeg 与 Python（`uv`）运行时及项目依赖。
- **全局挂载 MCP 服务与测试准则 (Rules)**：主动引导并自动将全局 MCP 服务与 **Artemis 移动端测试思维准则 (`rules.md`)** 挂载至你使用的 AI IDE（支持 **Antigravity**、**Cursor**、**Claude Code**、**Codex**、**Windsurf**、**VS Code**、**Cline/Roo**、**OpenClaw**）。

### macOS 与 Linux

```bash
# 1. 克隆代码仓库并进入目录
git clone https://github.com/google/artemis.git && cd artemis

# 2. 一键启动
./start.sh
```

### Windows PowerShell

```powershell
# 1. 克隆代码仓库并进入目录
git clone https://github.com/google/artemis.git
cd artemis

# 2. 一键启动
.\start.bat
```

> PowerShell 默认不会从当前目录查找可执行脚本，因此必须使用 `.\start.bat`，且命令末尾不要添加 `\`。如果使用传统命令提示符（CMD），则运行 `start.bat`。

> **提示**：启动后将自动在默认浏览器中打开 Web 控制台（`http://localhost:8000`），提供设备连接向导、实时投屏、任务演练与状态回放面板。你也可以通过命令行直接运行：`uv run artemis run "打开系统设置，找到电池选项并告诉我当前电量" --profile flash`。

<a id="mcp-setup"></a>
<a id="mcp"></a>
<details>
<summary><b>Codex / Antigravity / Claude Code / Windsurf MCP 配置（点击展开）</b></summary>

<br>

ARTEMIS 内置原生 **Model Context Protocol (MCP)** 服务。只需将以下配置加入你的 IDE 配置文件中，即可在编写代码时直接驱动真机：

### 1. 一键自动安装到 IDE（推荐）

运行 `./start.sh`（macOS/Linux）或 `.\start.bat`（Windows PowerShell）启动脚本时，会主动询问是否自动挂载全局 MCP 与测试行为准则（支持跳过并在之后随时手动执行以下命令挂载）：

```bash
# 一键安装全局 MCP 服务与 Rules 到 Antigravity / Jetski：
uv run artemis mcp --install antigravity

# 或一键安装到所有支持的 AI IDE（包括 Codex）：
uv run artemis mcp --install all
```

> **提示**：你也可以在首次运行 `uv run artemis init` 配置向导时，交互式完成 IDE 的 MCP 自动挂载。
> **进阶提示**：如果希望在任意目录下都不需要加 `uv run` 就能全局直接使用 `artemis` 命令，可在项目根目录下执行一次 `uv tool install -e .`。

### 2. 手动配置（可选）

如果你习惯手动复制配置，可运行 `uv run artemis mcp --generate-config <client>`（例如 `codex` 或 `antigravity`）获取对应的 TOML 或 JSON 配置。请将 `command` 填写为项目下 `.venv` 虚拟环境中的 Python 绝对路径，并将 `/path/to/artemis` 替换为项目实际路径：

* **Codex** (`~/.codex/config.toml`)：
```toml
[mcp_servers.artemis]
command = "/path/to/artemis/.venv/bin/python"
args = ["-m", "mcp_server"]
cwd = "/path/to/artemis"

[mcp_servers.artemis.env]
PYTHONUNBUFFERED = "1"
PYTHONPATH = "/path/to/artemis"
```

* **Antigravity** (`~/.gemini/jetski/mcp_config.json`)：
```json
{
  "mcpServers": {
    "artemis": {
      "command": "/path/to/artemis/.venv/bin/python",
      "args": ["-m", "mcp_server"],
      "cwd": "/path/to/artemis",
      "env": {
        "PYTHONUNBUFFERED": "1"
      },
      "tools": {
        "mobile_run_task": { "eager": true },
        "mobile_manage_task": { "eager": true },
        "mobile_get_device_state": { "eager": true },
        "mobile_inspect_trace": { "eager": true }
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
      "command": "/path/to/artemis/.venv/bin/python",
      "args": ["-m", "mcp_server"],
      "cwd": "/path/to/artemis"
    }
  }
}
```

### 3. 挂载智能体行为规范 Rules（强烈推荐）

为使 AI 编程助手具备资深移动端测试工程师的严谨思维，避免凭空臆测 UI 交互，我们提供了专属的测试思维行为规范文件 [`mcp_server/rules.md`](./mcp_server/rules.md)（涵盖**可运行代码原则与真机探索**、**Flash 与 Pro 任务路由策略**、**延迟与时间补偿机制**以及**“动态优先、坐标兜底”定位模式**）。

你可以将 [`mcp_server/rules.md`](./mcp_server/rules.md) 挂载或复制到你的 AI IDE 规则配置中：
* **Antigravity**：将 `rules.md` 内容添加至工作区规则（Workspace Rules）或全局规则设置中。
* **Claude Code**：运行 `artemis mcp --install claude` 自动安装规则至 `~/.claude/rules/artemis.md`（只安装到单一位置——Claude Code 会同时加载 `~/.claude/CLAUDE.md` 与 `~/.claude/rules/*.md`，重复安装会浪费上下文）。
* **Cursor**：将内容复制到 `.cursorrules` 文件或在 `.cursor/rules/artemis.mdc` 中创建新规则。
* **Codex**：将内容添加至 `~/.codex/AGENTS.md`（或当前生效的 `AGENTS.override.md`）。
* **Windsurf / OpenClaw**：将内容添加到工作区规则或全局 System Prompt 中。

> 更多规范设计细节与 MCP 架构说明，请参阅 [MCP Server 文档](./mcp_server/README.md)。

### 4. 在 IDE 中体验真机协同
在 Codex / Antigravity / Claude Code 对话框中直接输入：
> *"请帮我把刚刚修改的代码编译成 APK 并安装到手机上，打开登录页面输入测试账号，验证登录后是否有异常弹窗，并把最终页面截图回传。"*

</details>

<a id="python-sdk"></a>
<details>
<summary><b>Python SDK 集成（点击展开）</b></summary>

<br>

开发电脑只需安装零运行时依赖的薄客户端；ADB、Agent、模型与图像处理全部留在设备主机：

```powershell
uv add "artemis-client @ git+https://github.com/google/artemis.git#subdirectory=packages/artemis-client"
```

```python
import asyncio
from artemis_client import ArtemisClient


async def main():
    client = ArtemisClient(
        "http://artemis-host:8000",
        device_serial="emulator-5554",  # 可选：指定目标设备序列号（不传则自动选择空闲设备）
        default_profile="flash",  # "flash" 极速校验 或 "pro" 深度推理自愈
    )

    result = await client.run(
        "打开系统设置，进入『电池』页面，验证是否正常显示电量百分比，确认页面无异常报错弹窗。",
    )

    assert result.succeeded, f"测试执行失败: {result.error or result.status}"
    print(f"✅ 测试通过！设备: {result.device_serial} | Trace ID: {result.trace_id}")


if __name__ == "__main__":
    asyncio.run(main())
```

</details>

## 使用方式

<p align="center">
  <img src="./docs/assets/artemis-ui-showcase.png" alt="Artemis 可视化控制台" width="100%" />
  <br />
  <sub><b>控制台功能概览</b>：<b>① 视图切换</b>（主页与工作区） · <b>② 运行模式与回放</b>（Flash/Pro 状态与视频回放） · <b>③ 实时感知推理流</b>（动作感知、目标坐标与结构化总结） · <b>④ 提示词输入坞 (Prompt Dock)</b>（自然语言下发） · <b>⑤ 任务队列看板</b>（生命周期与历史回溯）</sub>
</p>

* **Web 可视化测试控制台 (`uv run artemis ui`)**：集成设备实时投屏与交互面板，支持通过自然语言下发测试用例，实时观测推理步骤、操作轨迹、截图留存与异常状态回放；支持在任意终端使用 `uv run artemis restart`、`uv run artemis stop`、`uv run artemis status` 一键重启、关停或查看服务状态；
* **原生 MCP 协议 (IDE 协同)**：作为标准 MCP 服务器无缝接入 **Antigravity、Claude Code、Windsurf** 等开发环境，在 IDE 中直接驱动真机完成自动化测试与 Bug 复现验证；
* **开发者命令行 CLI (`uv run artemis run`)**：支持通过终端直接执行自动化测试用例、探索性稳定性巡检或 AndroidWorld 基准评测，提供高保真结构化终端输出；
* **Python SDK**：作为标准 Python 库无缝集成至现有自动化测试框架（如 pytest）或 CI/CD 流水线，提供基于 Pydantic 的强类型结构化结果与断言支持。

<a id="benchmarks"></a>
<a id="基准评测"></a>
## 基准评测：AndroidWorld (SOTA 99%+)

在 Google Research 发布的业界基准评测 [AndroidWorld](https://github.com/google-research/android_world)（涵盖 20+ 款常用应用与 100+ 项复杂多步长程任务）中：**Artemis 在全套长程任务评测中展现了高鲁棒性，取得了超过 99% 的任务完成率。**

<p align="center">
  <img src="./docs/assets/androidworld_leaderboard.png?v=2" alt="AndroidWorld 评测基准对比" width="100%" />
</p>

## ARTEMIS 是如何建构的

* **Pre-Touch 触控前像素守门与预测性连点**：彻底杜绝大模型推理延迟造成的「静默误触」。动作下发前毫秒级拦截意外弹窗（0 Token、0 云端等待），Micro-ROI 局部校验目标稳定性；针对视频全屏等瞬态控件，结合历史先验毫秒级连击唤醒，在淡出窗口期内瞬时闭环；
* **三层递进式目标定位引擎 (Progressive Grounding)**：首层通过本地 OCR 与无障碍树几何融合（~150ms、0 Token），以无偏移数字索引承载 85%+ 常见交互；二层对 Flutter/Compose/Canvas 等自绘组件自动回退至空间视觉模型，三层辅以沙箱 CV 探针核验细微像素状态；
* **弹性双执行引擎与运行时上下文动态压缩**：兼顾 3–5 秒秒级响应的 CI 高吞吐回归（Flash 模式）与深度长程认知状态图探索（Pro 模式），后台异步生成视觉增量并剪除冗余 DOM，长程任务 Token 消耗锐降 70%+，支持 10+ 小时无人值守稳定性压测。

<p align="center">
  <img src="./docs/assets/artemis_architecture_diagram.png" alt="ARTEMIS 架构系统示意图" width="100%" />
</p>

## 运行模式：Flash vs. Pro

ARTEMIS 提供两种运行模式以适应不同的自动化需求：

* **Flash 模式 (`--profile flash`)**：轻量极速且省 Token 的响应式循环（单步约 3–5 秒）：单个模型观察实时屏幕、思考、执行，不经过图编排，适合常规确定性 UI 操作。默认不限步数（`agent.flash.max_turns`，0 表示不限），因为历史是被压缩而不是被截断：Flash 与 Pro 共用同一套会话记录账本（相对测试时间 `T+mm:ss`、截图折叠为视觉摘要、更早的步骤分块归档并可通过 `search_history` / `replay_steps` 按需召回），并可调用 `video_analyzer` 分析整段会话录屏；对自动消失的控制栏、toast 等瞬态控件，用 `click_sequence` 把多次点击串成一个原子序列。*局限性*：没有任务计划与笔记、没有执行前安全网、没有检查点校验与最终报告、不能执行 ADB 命令。
* **Pro 模式 (`--profile pro`)**：最强大的架构（单步约 15–40 秒），由多智能体图编排：**Planner** 维护一份带里程碑和 `verify` / `assert` 检查项的 Markdown 任务计划；**Operator** 按计划执行，拥有全套工具（Explorer 元素定位，其 `flash` / `pro` / `ultra` 三档感知深度是按运行模式设置的用户配置——`config/artemis.jsonc` 里的 `pro.explorer.mode` / `flash.explorer_mode` 或 `--explorer-pro-mode`——智能体自身不会选择档位；笔记、历史召回、视频分析、ADB 诊断）。单个动作执行前都经过 **Safety Net** 校验（XML 优先、像素兜底），而一轮里的多个动作则作为**快速动作序列 (fast-action burst)** 连发，赶在瞬态控件消失前完成操作。动作被拦截或失败时会打开一条**执行事故 (execution incident)**，持续留在 Operator 的上下文里直到后续动作执行成功，恢复由 Operator 自己完成，不再有独立的修复智能体。只读的 **Checker** 校验计划中的检查点，并在退出前对照原始目标做终审（`--verification-level`：`off` / `final`（默认）/ `checkpoints` / `strict`），计划里程碑的改动会得到建议式复核。支持 100+ 步的长程复杂业务流、`[Loop:continuous]` 持续监控以及可选的书面报告。

## 路线图

- [ ] **Android Studio 深度集成**：推出官方 IDE 插件与协同工作流，支持在 Android Studio 内直接进行自动化测试、设备交互与断点调试。
- [ ] **iOS 跨平台支持**：将视觉感知与自动化执行引擎拓展至 iOS 真机与模拟器。
- [ ] **端侧轻量化模型**：支持离线运行的轻量级 Edge VLM，实现低延迟与隐私安全的本地自动化。
- [ ] **实时语音双工交互**：支持自然语音下发任务与实时打断（Barge-in）控制。

## 社区与贡献

欢迎通过以下方式参与项目建设：
* **Star 本项目**以关注最新进展与更新
* 加入 [Discord 社区](https://discord.gg/wF2FN4WHGY) 参与技术探讨与功能建议
* 提交 [Issue](https://github.com/google/artemis/issues) 反馈 Bug，欢迎发起 [Pull Request](https://github.com/google/artemis/pulls) 贡献代码

## 开源许可证

本项目基于 [Apache License 2.0](LICENSE) 协议开源。
