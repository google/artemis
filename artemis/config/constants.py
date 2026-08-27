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

"""Centralized constants, environment variable names, and type definitions for ARTEMIS."""

from typing import Literal

# ==============================================================================
# Environment Variable Keys
# ==============================================================================

# Application & Workspace Paths
ENV_ARTEMIS_APP_DIR = "ARTEMIS_APP_DIR"
ENV_ANTIGRAVITY_APP_DIR = "ANTIGRAVITY_APP_DIR"
ENV_ARTEMIS_USE_USER_DIR = "ARTEMIS_USE_USER_DIR"
ENV_ARTEMIS_TRACES_DIR = "ARTEMIS_TRACES_DIR"
ENV_DATA_ENGINE_DB_PATH = "DATA_ENGINE_DB_PATH"

# Cloud Brain & Distributed Execution
ENV_ARTEMIS_CLOUD_MODE = "ARTEMIS_CLOUD_MODE"
ENV_ARTEMIS_CLOUD_SESSION_ID = "ARTEMIS_CLOUD_SESSION_ID"
ENV_ARTEMIS_CLOUD_GATEWAY_URL = "ARTEMIS_CLOUD_GATEWAY_URL"
ENV_ARTEMIS_TENANT_ID = "ARTEMIS_TENANT_ID"
ENV_ARTEMIS_TENANT_TOKEN = "ARTEMIS_TENANT_TOKEN"
ENV_ARTEMIS_EDGE_PORT = "ARTEMIS_EDGE_PORT"

# Runtime State & IPC
ENV_ARTEMIS_IPC_PORT = "ARTEMIS_IPC_PORT"
ENV_ANTIGRAVITY_LS_ADDRESS = "ANTIGRAVITY_LS_ADDRESS"
ENV_ARTEMIS_MCP_SERVER = "ARTEMIS_MCP_SERVER"

# Device & ADB
ENV_ADB_DEVICE_SERIAL = "ADB_DEVICE_SERIAL"
ENV_ARTEMIS_DEVICE_ID = "ARTEMIS_DEVICE_ID"
ENV_ADB_HOST = "ADB_HOST"
ENV_ADB_PORT = "ADB_PORT"
ENV_ADB_SERVER_SOCKET = "ADB_SERVER_SOCKET"

# LLM & Vision OCR API Keys
ENV_GOOGLE_API_KEY = "GOOGLE_API_KEY"
ENV_GEMINI_API_KEY = "GEMINI_API_KEY"
ENV_GCP_API_KEY = "GCP_API_KEY"
ENV_OCR_API_KEY = "OCR_API_KEY"
ENV_VISION_API_KEY = "VISION_API_KEY"
ENV_API_KEY = "API_KEY"
ENV_OPENAI_API_KEY = "OPENAI_API_KEY"
ENV_OPENAI_BASE_URL = "OPENAI_BASE_URL"
ENV_ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"
ENV_OPEN_ROUTER_API_KEY = "OPEN_ROUTER_API_KEY"
ENV_XAI_API_KEY = "XAI_API_KEY"

# Explorer & Agent Defaults
ENV_ARTEMIS_EXPLORER_VERSION = "ARTEMIS_EXPLORER_VERSION"
ENV_ARTEMIS_DEFAULT_PROFILE = "ARTEMIS_DEFAULT_PROFILE"
ENV_ARTEMIS_DEFAULT_MODEL = "ARTEMIS_DEFAULT_MODEL"
ENV_ARTEMIS_USE_FILE_API = "ARTEMIS_USE_FILE_API"

# Debugging & Output Paths
ENV_KEEP_VIDEOS = "KEEP_VIDEOS"
ENV_ARTEMIS_DEBUG = "ARTEMIS_DEBUG"
ENV_EVENTS_OUTPUT_PATH = "EVENTS_OUTPUT_PATH"
ENV_RESULTS_OUTPUT_PATH = "RESULTS_OUTPUT_PATH"


# ==============================================================================
# Configuration Filenames
# ==============================================================================

ARTEMIS_CONFIG_FILENAME = "artemis.jsonc"
LLM_CONFIG_FILENAME = "llm-config.json"
LLM_CONFIG_OVERRIDE_FILENAME = "llm-config.override.jsonc"
AGENT_CONFIG_FILENAME = "agent_config.json"
DATA_ENGINE_DB_FILENAME = "data_engine.db"
IPC_PORT_FILENAME = ".artemis_ipc_port"
LS_ADDRESS_FILENAME = ".jetski_ls_address"
SERVER_INFO_FILENAME = ".artemis_server.json"
PAUSE_FILENAME = ".artemis_paused"
REPLAY_DIRNAME = "replay"
TEST_DATA_DIRNAME = "data"
TEST_OUTPUTS_DIRNAME = "outputs"
IMAGES_DIRNAME = "images"
DOTENV_FILENAME = ".env"


# ==============================================================================
# Default Values & Network Ports
# ==============================================================================

DEFAULT_ADB_HOST = "127.0.0.1"
DEFAULT_ADB_PORT = 5037
DEFAULT_GATEWAY_PORT = 8000
DEFAULT_EDGE_MONITOR_PORT = 8000
DEFAULT_ACTION_TIMEOUT = 30.0
DEFAULT_STREAM_PING_INTERVAL = 15.0

DEFAULT_PROFILE = "pro"
DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_EXPLORER_VERSION: Literal["flash", "pro", "ultra"] = "flash"


# ==============================================================================
# Literals and Type Aliases
# ==============================================================================

LLMProvider = Literal[
    "openai", "google", "openrouter", "xai", "vertexai", "anthropic", "ollama", "vllm", "custom"
]
ExplorerVersion = Literal["flash", "pro", "ultra"]

LLMUtilsNode = Literal[
    "outputter",
    "hopper",
    "video_analyzer",
    "object_detector",
]
LLMUtilsNodeWithFallback = LLMUtilsNode

AgentNode = Literal[
    "planner",
    "summarizer",
    "operator",
    "operator_summarizer",
    "log_reader_sub_agent",
    "log_analyzer",
    "diagnoser",
    "validator_failure_analyzer",
    "checker",
    "planner_avatar",
    "history_analyzer_expert",
    "diagnoser_expert",
    "explorer",
    "history_analyzer",
    "validator_pixel_safety_net",
    "validator",
    "output_analyzer",
]
AgentNodeWithFallback = AgentNode
