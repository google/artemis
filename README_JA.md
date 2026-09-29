<p align="center">
  <img src="./docs/assets/artemis-banner.png?v=7" alt="ARTEMIS Banner" width="100%" />
</p>

<p align="center">
  <strong>AI アシスタントとテストスイートが、人間と同じように実機を操作する。</strong>
</p>

<p align="center">
  <a href="./README.md">English</a> •
  <a href="./README_CN.md">中文文档</a> •
  <a href="./README_JA.md"><b>日本語</b></a> •
  <a href="#workflow-showcase">ワークフロー紹介</a> •
  <a href="#quick-start">クイックスタート</a> •
  <a href="#mcp-setup">IDE 向け MCP</a> •
  <a href="#benchmarks">ベンチマーク</a> •
  <a href="https://discord.gg/wF2FN4WHGY">Discord コミュニティ</a>
</p>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.12+-3776AB.svg?logo=python&logoColor=white" alt="Python 3.12+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-blue.svg" alt="License: Apache-2.0"></a>
  <a href="https://modelcontextprotocol.io/"><img src="https://img.shields.io/badge/MCP-Native%20Server-8A2BE2.svg" alt="MCP Native"></a>
  <a href="https://ai.google.dev/"><img src="https://img.shields.io/badge/Multimodal-Gemini%20%7C%20Claude%20%7C%20GPT--4o%20%7C%20Qwen--VL-4285F4.svg" alt="Multi-Model"></a>
  <a href="https://github.com/google-research/android_world"><img src="https://img.shields.io/badge/AndroidWorld-99%25%2B%20SOTA-success.svg" alt="AndroidWorld SOTA"></a>
</p>

<!-- デモ紹介 -->
<p align="center">
  <img src="./docs/assets/demo.gif" alt="Artemis の動作デモ" width="100%" />
  <br>
  <em>ライブデモ：Google マップで運転ルートを設定して所要時間の合計を計算し、続いて YouTube を開いて Coldplay の曲を再生します。</em>
</p>

## 主な特長

* **クロスアプリ自動化**：自然言語の指示から、Android 上でテストワークフローや日常的なタスクを実行します。
* **マルチモーダルな要素特定**：利用できる場合は要素インデックスを使い、カスタム UI には座標および画像ベースの特定をフォールバックとして併用します。
* **IDE 診断**：**Model Context Protocol (MCP)** 連携により、**Antigravity、Claude Code、Windsurf** からテスト端末を操作し、**Logcat** の出力やスクリーンショットを収集できます。
* **Flash 実行**：観察と操作を繰り返すリアクティブなループに非同期の履歴要約を組み合わせ、通常 **1 ステップあたり 3〜5 秒**で動作します。
* **Pro 探索**：個々の操作の前にターゲットを検証し、ブロックされた操作は Operator に差し戻して復旧させます。長時間の探索テストや安定性テストにも対応します。
* **AndroidWorld の実績**：Google Research の **AndroidWorld** ベンチマーク（100 以上のマルチステップタスク）で **99% 以上のタスク完了率**を達成しました。

<a id="workflow-showcase"></a>
## Antigravity × ARTEMIS：自律テストのワークフロー

**Antigravity** は MCP を介して **ARTEMIS** を利用し、テスト依頼を計画・実機実行・診断レポートへと変換します。

<table width="100%">
  <tr>
    <td width="50%" align="center">
      <b>1. プロンプト入力（タスクの割り当て）</b><br>
      <sub>テストシナリオと目標指標を Antigravity で記述</sub><br><br>
      <img src="./docs/assets/workflow-1-prompt.png" width="100%" alt="ステップ 1：Antigravity でのプロンプト入力" />
    </td>
    <td width="50%" align="center">
      <b>2. テスト計画の生成</b><br>
      <sub>レビュー用に、手順ごとのテスト計画と構成を立案</sub><br><br>
      <img src="./docs/assets/workflow-2-plan.png" width="100%" alt="ステップ 2：テスト計画の生成" />
    </td>
  </tr>
  <tr>
    <td width="50%" align="center">
      <b>3. 自律的なテスト実行</b><br>
      <sub>実機を操作し、UI を辿りながらパフォーマンスを計測</sub><br><br>
      <img src="./docs/assets/workflow-3-exec.png" width="100%" alt="ステップ 3：自律的なテスト実行" />
    </td>
    <td width="50%" align="center">
      <b>4. 最終レポート</b><br>
      <sub>構造化された監査結果、指標テーブル、生データを出力</sub><br><br>
      <img src="./docs/assets/workflow-4-report.png" width="100%" alt="ステップ 4：最終レポート" />
    </td>
  </tr>
</table>

<a id="quick-start"></a>
## クイックスタート

**USB デバッグ**を有効にした Android 実機、またはエミュレータを接続してください。ワンクリック起動スクリプトが以下を自動で行います。
- **システムツールチェーンのインストール**：ADB、scrcpy、FFmpeg、Python（`uv`）の依存関係を検出し、自動でインストールします。
- **グローバル MCP サーバーと AI エージェント用ルールの導入**：グローバル MCP 設定と **Artemis モバイルテスト・マインドセット（`rules.md`）** を AI IDE（**Antigravity**、**Cursor**、**Claude Code**、**Codex**、**Windsurf**、**VS Code**、**Cline/Roo**、**OpenClaw**）へ自動インストールするか確認します。

### macOS および Linux

```bash
# 1. リポジトリをクローンしてディレクトリへ移動
git clone https://github.com/google/artemis.git && cd artemis

# 2. ワンクリック起動
./start.sh
```

### Windows PowerShell

```powershell
# 1. リポジトリをクローンしてディレクトリへ移動
git clone https://github.com/google/artemis.git
cd artemis

# 2. ワンクリック起動
.\start.bat
```

> PowerShell は既定でカレントディレクトリから実行可能スクリプトを探さないため、末尾に `\` を付けずに `.\start.bat` を使用してください。コマンドプロンプト（CMD）では代わりに `start.bat` を使用します。

> **ヒント**：既定のブラウザで `http://localhost:8000` が開き、デバイス接続ウィザード、画面のライブミラーリング、プロンプトのサンドボックス、実行リプレイを利用できます。CLI から直接実行することもできます：`uv run artemis run "設定を開いてバッテリーを探し、現在の残量を教えて" --profile flash`。

<a id="mcp-setup"></a>
<a id="mcp"></a>
<details>
<summary><b>Codex / Antigravity / Claude Code / Windsurf 向け MCP セットアップ（クリックで展開）</b></summary>

<br>

ARTEMIS はネイティブの **Model Context Protocol (MCP)** サーバーを内蔵しています。実機をそのまま AI IDE に接続できます。

### 1. ワンクリック自動インストール（推奨）

`./start.sh`（macOS/Linux）または `.\start.bat`（Windows PowerShell）を実行すると、検出された IDE 向けにグローバル MCP とテスト用ルールを設定するか確認されます（以下のコマンドで、後からいつでも手動でインストール・更新できます）。

```bash
# Antigravity / Jetski 向けに MCP サーバーとグローバルルールを自動インストール：
uv run artemis mcp --install antigravity

# または、対応するすべての AI IDE（Codex を含む）にインストール：
uv run artemis mcp --install all
```

> **ヒント**：初回セットアップ時に `uv run artemis init` から対話形式で MCP を設定することもできます。
> **上級者向けヒント**：`uv run` を付けずに任意のディレクトリから `artemis` コマンドを使いたい場合は、プロジェクトルートで `uv tool install -e .` を一度実行してください。

### 2. 手動設定（任意）

手動で設定したい場合は、`uv run artemis mcp --generate-config <client>`（例：`codex` や `antigravity`）を実行すると、対応する TOML または JSON のスニペットが出力されます。`/path/to/artemis` は実際のリポジトリのパスに置き換え、`command` には `.venv` の Python 実行ファイルを指定してください。

* **Codex**（`~/.codex/config.toml`）:
```toml
[mcp_servers.artemis]
command = "/path/to/artemis/.venv/bin/python"
args = ["-m", "mcp_server"]
cwd = "/path/to/artemis"

[mcp_servers.artemis.env]
PYTHONUNBUFFERED = "1"
PYTHONPATH = "/path/to/artemis"
```

* **Antigravity**（`~/.gemini/jetski/mcp_config.json`）:
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
        "mobile_inspect_trace": { "eager": true },
        "mobile_diagnose": { "eager": true }
      }
    }
  }
}
```

* **Claude Desktop**（`claude_desktop_config.json`）:
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

### 3. AI エージェント向け行動ルールの導入（強く推奨）

AI コーディングアシスタントがシニアモバイルテストエンジニアと同じ厳密さで動作し、UI 操作をハルシネーションしないようにするため、専用のテスト・マインドセットのルールファイルを [`mcp_server/rules.md`](./mcp_server/rules.md) に用意しています（**コーディング前の能動的な探索**、**Flash と Pro の使い分け戦略**、**レイテンシとタイミングの補正**、**「動的優先・座標フォールバック」のロケーターパターン**を扱います）。

[`mcp_server/rules.md`](./mcp_server/rules.md) は、お使いの AI IDE のルール設定に取り込む（またはコピーする）ことができます。
* **Antigravity**：`rules.md` の内容を Workspace Rules、Global Rules の設定、またはエージェントの指示に追加します。
* **Claude Code**：`artemis mcp --install claude` を実行すると、ルールが `~/.claude/rules/artemis.md` にインストールされます（インストール先は 1 か所だけにしてください。Claude Code は `~/.claude/CLAUDE.md` と `~/.claude/rules/*.md` の両方を読み込むため、ルールを重複させるとコンテキストが無駄になります）。
* **Cursor**：内容を `.cursorrules` にコピーするか、`.cursor/rules/artemis.mdc` にルールファイルを作成します。
* **Codex**：内容を `~/.codex/AGENTS.md`（または有効な `AGENTS.override.md`）に追加します。
* **Windsurf / OpenClaw**：ワークスペースのルールまたはグローバルのシステムプロンプトにルールを追加します。

> テスト・マインドセットと MCP アーキテクチャの詳細については、[MCP サーバーの README](./mcp_server/README.md) を参照してください。

### 4. IDE のチャットから実機に指示する
Codex、Antigravity、Claude Code で、次のように指示するだけです。
> *「最新の変更を APK にビルドし、接続中の端末にインストールして、テストアカウントでログイン画面を開き、ログイン後に想定外のポップアップが出ないか確認して、最終画面のスクリーンショットを返して。」*

</details>

<a id="python-sdk"></a>
<details>
<summary><b>Python SDK 連携（クリックで展開）</b></summary>

<br>

ランタイム依存ゼロのクライアントを開発マシンにインストールします。ADB、エージェント、モデル、画像処理はデバイスホスト側に残ります。

```powershell
uv add "artemis-client @ git+https://github.com/google/artemis.git#subdirectory=packages/artemis-client"
```

```python
import asyncio
from artemis_client import ArtemisClient


async def main():
    client = ArtemisClient(
        "http://artemis-host:8000",
        device_serial="emulator-5554",  # 任意：対象デバイスのシリアルを指定
        default_profile="flash",  # "flash"（高速リアクティブ）または "pro"（深い推論）
    )

    result = await client.run(
        "システム設定を開き、「バッテリー」に移動して、バッテリー残量が表示されていることを確認し、クラッシュダイアログが出ていないか調べて。",
    )

    assert result.succeeded, f"Test failed: {result.error or result.status}"
    print(f"✅ Test Passed! Device: {result.device_serial} | Trace ID: {result.trace_id}")


if __name__ == "__main__":
    asyncio.run(main())
```

</details>

## 利用形態

<p align="center">
  <img src="./docs/assets/artemis-ui-showcase-en.png" alt="Artemis Web コンソール" width="100%" />
  <br />
  <sub><b>コンソール概要</b>：<b>① ビュー切り替え</b>（Home / Workspace）・<b>② モデルとリプレイ</b>（Flash/Pro のステータスと動画リプレイ）・<b>③ エージェントのライブストリーム</b>（アクションの認識、対象座標、構造化された結果）・<b>④ プロンプトドック</b>（自然言語での指示）・<b>⑤ タスクキューとダッシュボード</b>（ライフサイクルと履歴）</sub>
</p>

* **Web ビジュアルテストコンソール（`uv run artemis ui`）**：リアルタイムの画面投影と操作パネルを備え、自然言語でのテスト指示、推論テレメトリのライブ表示、アクションの軌跡、実行リプレイに対応します。サーバーのライフサイクルは、任意のターミナルから `uv run artemis restart`、`uv run artemis stop`、`uv run artemis status` でいつでも管理できます。
* **MCP サーバー**：**Antigravity、Claude Code、Windsurf** などの MCP クライアントを実機に接続し、バグの再現やテスト実行を行います。
* **開発者向け CLI（`uv run artemis run`）**：自動テストケース、探索的な安定性チェック、AndroidWorld ベンチマークをターミナルから直接実行し、忠実度の高い構造化出力を得られます。
* **Python SDK**：既存の自動テストフレームワーク（pytest など）や CI/CD パイプラインに標準的な Python ライブラリとして組み込めます。Pydantic による厳密に型付けされた構造化出力とアサーションをサポートします。

<a id="on-device-helper"></a>
## ARTEMIS が端末にインストールするもの

デバイス上で最初のタスクを実行すると、**Artemis Accessibility Helper** がインストールされます。これは UiAutomation の接続を占有せずに画面レイアウトを読み取る、小さなアクセシビリティサービスです。UiAutomation を使うツールは、`FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES` を有効にしない限りこのヘルパーを抑制することがあります。折りたたまれた「Artemis test helper is running」という通知と、設定 > ユーザー補助に新しい項目が表示されますが、どちらもこのヘルパーです。端末内でのみ動作し、外部には何も送信しません。

* 事前インストール（最初のタスクでの約 3 秒の遅延を回避）：`uv run artemis helper install`
* 状態の確認：`uv run artemis helper status` / `uv run artemis doctor`
* いつでも削除可能：`uv run artemis helper uninstall`
* 代わりに UIAutomator2 を使う：`.env` に `ARTEMIS_HIERARCHY_BACKEND=uiautomator`
* 自動インストールを無効化：`.env` に `ARTEMIS_HELPER_AUTO_INSTALL=false`

タスクの途中でヘルパーが失敗した場合、ARTEMIS は UIAutomator2 にフォールバックし、その旨をタスクのタイムライン、`mobile_manage_task` のステータス、最終レポートに記録します。

<a id="benchmarks"></a>
## ベンチマーク：AndroidWorld（SOTA 99% 以上）

Artemis は、20 以上のアプリと 100 以上のマルチステップタスクを網羅する Google Research のベンチマーク [AndroidWorld](https://github.com/google-research/android_world) で、**99% 以上の完了率**を達成しました。

<p align="center">
  <img src="./docs/assets/androidworld_leaderboard.png?v=2" alt="AndroidWorld ベンチマーク比較" width="100%" />
</p>

## ARTEMIS のアーキテクチャ

* **実行前チェックとアクションバースト**：Pro は個々の操作を実行する前に、対象をライブの UI ツリーとピクセルの両面から検証します。アクションバーストにより、次のモデル応答を待たずに一時的なコントロールを処理できます。
* **要素の特定**：アクセシビリティ階層と OCR を視覚モデルと組み合わせ、カスタムの Canvas、Compose、Flutter の UI にも対応します。
* **共有履歴の圧縮**：Flash と Pro は古いスクリーンショットを視覚的な要約に置き換え、完了したステップを検索可能な履歴チャンクへ圧縮します。生のターンをいつ置き換えるかはコンテキストのしきい値で制御されます。

<p align="center">
  <img src="./docs/assets/artemis_architecture_diagram.png" alt="ARTEMIS システムアーキテクチャ図" width="100%" />
</p>

## 実行プロファイル：Flash と Pro

ARTEMIS は、自動化の要件に応じて 2 つの実行プロファイルを提供します。

* **Flash プロファイル（`--profile flash`）**：高速かつトークン効率の良いリアクティブループ（1 ステップあたり約 3〜5 秒）。1 つのモデルがライブ画面を観察し、思考し、操作します。グラフによるオーケストレーションはありません。定型的で決定的な UI タスクに最適です。履歴は上限で打ち切るのではなく圧縮されるため、ループは既定で無制限です（`agent.flash.max_turns`、0 = 無制限）。Flash は Pro のセッショントランスクリプト台帳（セッション相対の `T+mm:ss` クロック、視覚的な要約に畳み込まれたスクリーンショット、時代ごとのチャンクにまとめられ `search_history` / `replay_steps` で必要に応じて呼び出せる過去のステップ）を共有し、`video_analyzer` からセッション録画を照会できます。自動的に消えるコントロールバーやトーストのような一時的な UI は、タップを 1 つの `click_sequence` に連結して処理します。*制限事項*：タスク計画やメモはなく、実行前のセーフティネット、チェックポイント検証、最終レポート、ADB シェルもありません。
* **Pro プロファイル（`--profile pro`）**：計画と検証を行うワークフロー（1 ステップあたり約 15〜40 秒）で、マルチエージェントのグラフとして構築されています。**Planner** がマイルストーンと `verify` / `assert` のチェック項目を含む、更新され続ける Markdown のタスク計画を管理し、**Operator** がフルセットのツール（Explorer によるグラウンディング。その `flash` / `pro` / `ultra` ティアはプロファイルごとのユーザー設定であり、`config/artemis.jsonc` の `pro.explorer.mode` / `flash.explorer_mode` または `--explorer-pro-mode` で指定します。エージェントが選ぶことはありません。加えてメモ、履歴の呼び出し、動画分析、ADB 診断）を使って実行します。すべての操作は実行前に**セーフティネット**（XML を優先し、ピクセルをフォールバック）を通過し、複数操作の**高速アクションバースト**は連続して発行され、一時的な UI に対するターンのレイテンシを回避します。ブロックまたは失敗した操作は**実行インシデント**を発生させ、後続の操作が成功するまで Operator のコンテキストに残るため、専用の修復エージェントを設けずに Operator 自身が復旧を担います。読み取り専用の **Checker** が計画のチェックポイントを検証し、終了時には元のゴールに対する最終レビューを実施します（`--verification-level`：`off` / `final`（既定） / `checkpoints` / `strict`）。また、計画のマイルストーンの編集にはアドバイザリーレビューが行われます。100 ステップを超える長期的なワークフロー、`[Loop:continuous]` による監視、任意の書面レポートに対応します。

## ロードマップ

- [ ] **Android Studio 連携**：ネイティブの IDE プラグインとワークフロー統合により、Android Studio 内でのデバッグ、テストの記録、デバイスの自動制御を可能にします。
- [ ] **iOS プラットフォームへの拡張**：マルチモーダルな認識とモバイル自動化を iOS の実機およびシミュレータへ広げます。
- [ ] **オンデバイスの軽量 VLM**：軽量なエッジ向け視覚モデルによるローカル実行で、低レイテンシかつプライバシー重視の自動化を実現します。
- [ ] **リアルタイム双方向音声インタラクション**：音声によるタスク指示と、リアルタイムの対話制御・割り込み処理に対応します。

## コミュニティと貢献

コントリビューションを歓迎します！
* リポジトリに **Star** を付けて、更新やリリースをフォローしてください
* 技術的な議論には [Discord コミュニティ](https://discord.gg/wF2FN4WHGY) にご参加ください
* [Issue](https://github.com/google/artemis/issues) の作成や [Pull Request](https://github.com/google/artemis/pulls) の送信もお待ちしています

## ライセンス

本プロジェクトは [Apache License 2.0](LICENSE) の下で提供されています。

本プロジェクトには [Minitap, Inc.](https://github.com/minitap-ai/mobile-use) が開発したソースコードが含まれています。
