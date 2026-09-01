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

"""System Readiness & Diagnostics Router for Artemis Admin Console."""

import ipaddress
import os
import secrets
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from artemis.core.diagnostics import readiness_engine
from artemis.core.diagnostics.adb_server_connection import (
    InvalidAdbServerEndpoint,
    adb_server_connection,
)
from artemis.core.diagnostics.schema import SystemReadinessReport

router = APIRouter(prefix="/api/system", tags=["system"])


def _require_local_admin_request(request: Request) -> None:
    """Keep endpoint probing and mutation on the local administration boundary."""
    client_host = request.client.host if request.client else None
    allow_remote = os.getenv("ARTEMIS_ALLOW_REMOTE_ADB_CONFIGURATION", "").lower() in {
        "1",
        "true",
        "yes",
    }
    if client_host and not allow_remote:
        try:
            is_loopback = ipaddress.ip_address(client_host).is_loopback
        except ValueError:
            is_loopback = client_host.lower() == "localhost"
        if not is_loopback:
            raise HTTPException(
                status_code=403,
                detail=(
                    "ADB server settings are local-only. Set "
                    "ARTEMIS_ALLOW_REMOTE_ADB_CONFIGURATION=true to manage them from another "
                    "computer."
                ),
            )

    origin = request.headers.get("origin")
    host = request.headers.get("host")
    if not origin or not host:
        return
    origin_host = urlsplit(origin).netloc.lower()
    if origin_host != host.lower():
        raise HTTPException(
            status_code=403,
            detail="ADB server settings can only be changed from the Artemis console.",
        )


def _require_loopback_request(request: Request, detail: str) -> None:
    """Reject requests whose TCP peer is not the local machine."""
    client_host = request.client.host if request.client else None
    try:
        is_loopback = bool(client_host and ipaddress.ip_address(client_host).is_loopback)
    except ValueError:
        is_loopback = bool(client_host and client_host.lower() == "localhost")
    if not is_loopback:
        raise HTTPException(status_code=403, detail=detail)


def _require_local_lifecycle_request(request: Request) -> None:
    """Authorize a process-lifecycle request from the local CLI only."""
    _require_loopback_request(request, "Server lifecycle controls are local-only.")

    expected = getattr(request.app.state, "lifecycle_token", None)
    supplied = request.headers.get("x-artemis-lifecycle-token")
    if not (
        isinstance(expected, str)
        and isinstance(supplied, str)
        and secrets.compare_digest(expected, supplied)
    ):
        raise HTTPException(status_code=403, detail="Invalid server lifecycle token.")


class SelectDeviceRequest(BaseModel):
    """Payload to select an active target Android device."""

    serial: str = Field(description="Serial number or identifier of the Android device to select")


@router.get("/readiness", response_model=SystemReadinessReport)
async def get_system_readiness(force: bool = False) -> SystemReadinessReport:
    """Execute all diagnostic probes and return a comprehensive system readiness report."""
    return await readiness_engine.run_all(force_refresh=force)


@router.post("/devices/select")
async def select_active_device(request: SelectDeviceRequest):
    """Select the active Android device or emulator for subsequent automated tasks."""
    serial = request.serial.strip()
    if not serial:
        raise HTTPException(status_code=400, detail="Device serial cannot be empty.")

    readiness_engine.set_probe_target_serial(serial)
    # Return updated readiness
    report = await readiness_engine.run_all(force_refresh=True)
    return {
        "status": "success",
        "selected_serial": serial,
        "report": report,
    }


@router.post("/adb/restart")
async def restart_adb_server():
    """Restart local ADB server and return an updated readiness check."""
    restart_result = await readiness_engine.restart_adb_server()
    readiness_engine.invalidate_cache()
    updated_report = await readiness_engine.run_all(force_refresh=True)
    return {
        "restart_result": restart_result,
        "report": updated_report,
    }


@router.post("/adb/heal-keys")
async def heal_adb_keys():
    """Auto-heal corrupted ADB authentication RSA keys and return updated readiness."""
    heal_result = await readiness_engine.heal_adb_keys()
    updated_report = await readiness_engine.run_all()
    return {
        "heal_result": heal_result,
        "report": updated_report,
    }


class ConnectAdbRequest(BaseModel):
    """Payload to connect to an Android device over Wi-Fi."""

    host: str = Field(description="IP address of Android device")
    port: int = Field(default=5555, description="Port number")


@router.post("/adb/connect")
async def connect_wireless_adb(request: ConnectAdbRequest):
    """Connect to a device over Wi-Fi and return updated readiness."""
    connect_result = await readiness_engine.connect_wireless_adb(request.host, request.port)
    readiness_engine.invalidate_cache()
    updated_report = await readiness_engine.run_all(force_refresh=True)
    return {
        "connect_result": connect_result,
        "report": updated_report,
    }


class ConnectAdbServerRequest(BaseModel):
    """Payload to select an ADB server endpoint accessible from this computer."""

    host: str = Field(description="Host name or IP address of the ADB server")
    port: int = Field(default=5037, ge=1, le=65535, description="ADB server port")
    persist: bool = Field(default=True, description="Persist the endpoint for future launches")


@router.get("/adb/server")
async def get_adb_server_status():
    """Return the process-wide ADB server endpoint currently used by Artemis."""
    return adb_server_connection.status()


@router.post("/adb/server/connect")
async def connect_adb_server(payload: ConnectAdbServerRequest, request: Request):
    """Validate and activate an ADB server endpoint."""
    _require_local_admin_request(request)
    try:
        connection_result = await adb_server_connection.connect(
            payload.host,
            payload.port,
            persist=payload.persist,
        )
    except InvalidAdbServerEndpoint as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    response: dict[str, object] = {"connection_result": connection_result}
    if connection_result["success"]:
        readiness_engine.set_probe_target_serial(None)
        readiness_engine.invalidate_cache()
        response["report"] = await readiness_engine.run_all(force_refresh=True)
    return response


@router.post("/adb/server/probe")
async def probe_adb_server(payload: ConnectAdbServerRequest, request: Request):
    """Test an ADB server endpoint without changing the active endpoint."""
    _require_local_admin_request(request)
    try:
        connection_result = await adb_server_connection.probe(payload.host, payload.port)
    except InvalidAdbServerEndpoint as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"connection_result": connection_result}


@router.post("/adb/server/local")
async def use_local_adb_server(request: Request, persist: bool = True):
    """Restore the standard local ADB server without touching a remote daemon."""
    _require_local_admin_request(request)
    connection_result = await adb_server_connection.use_local_server(persist=persist)
    readiness_engine.set_probe_target_serial(None)
    readiness_engine.invalidate_cache()
    updated_report = await readiness_engine.run_all(force_refresh=True)
    return {
        "connection_result": connection_result,
        "report": updated_report,
    }


class LaunchEmulatorRequest(BaseModel):
    """Payload to launch a local Android Virtual Device (AVD)."""

    avd_name: str = Field(
        description="Name of the installed AVD emulator to launch (e.g. Android_2)"
    )


@router.post("/emulator/launch")
async def launch_emulator(request: LaunchEmulatorRequest):
    """Launch an Android emulator in the background and return initiation status."""
    avd_name = request.avd_name.strip()
    if not avd_name:
        raise HTTPException(status_code=400, detail="AVD name cannot be empty.")

    launch_res = await readiness_engine.launch_emulator(avd_name)
    return launch_res


@router.get("/emulator/status")
async def get_emulator_status():
    """Query real-time progress and logs of background emulator launch."""
    return readiness_engine.get_emulator_status()


@router.post("/emulator/stop")
async def stop_emulator():
    """Stop active emulator process."""
    return await readiness_engine.stop_emulator()


@router.post("/emulator/dismiss")
async def dismiss_emulator():
    """Dismiss emulator launch tracking state."""
    return readiness_engine.dismiss_emulator()


class UpdateCredentialsRequest(BaseModel):
    """Payload to update and configure LLM or Vision OCR API credentials."""

    provider: str = Field(
        default="google",
        description="Provider identifier (e.g. google, gemini, openai, anthropic, openrouter, ocr)",
    )
    api_key: str = Field(description="The secret API key string to configure")
    persist_to_env: bool = Field(
        default=True, description="Whether to persist the key to .env file"
    )


class ValidateCredentialsRequest(BaseModel):
    """Payload to test and verify LLM or Vision OCR API credentials without saving."""

    provider: str = Field(
        default="google",
        description="Provider identifier (e.g. google, gemini, openai, anthropic, openrouter, ocr)",
    )
    api_key: str = Field(description="The secret API key string to test")
    base_url: str | None = Field(default=None, description="Optional custom base URL")


@router.get("/credentials")
async def get_credentials():
    """Report which providers have an API key configured.

    Secret values never leave the process: this endpoint intentionally returns
    presence booleans only. Keys are written via POST /credentials and used
    server-side.
    """
    from artemis.config import settings

    providers = ("google", "openai", "anthropic", "openrouter", "ocr")
    status = {name: bool(settings.get_api_key(name)) for name in providers}
    status["gemini"] = status["google"]
    return {"providers": [{"name": name, "configured": configured} for name, configured in status.items()]}


@router.post("/credentials/test")
async def test_credentials(request: ValidateCredentialsRequest):
    """Test and verify whether an API key is valid and usable with the corresponding provider endpoint."""
    from artemis.utils.credentials_validator import validate_api_key

    provider = request.provider.strip().lower()
    key = request.api_key.strip()

    if not key:
        raise HTTPException(status_code=400, detail="API key cannot be empty.")

    is_valid, message = await validate_api_key(
        provider=provider,
        api_key=key,
        base_url=request.base_url,
    )
    if not is_valid:
        raise HTTPException(status_code=400, detail=message)

    return {
        "valid": True,
        "provider": provider,
        "message": message,
    }


@router.post("/credentials")
async def update_credentials(request: UpdateCredentialsRequest):
    """Dynamically configure and persist LLM or Vision API key, returning updated readiness report."""
    from artemis.utils.credentials_validator import validate_api_key

    provider = request.provider.strip().lower()
    key = request.api_key.strip()

    # If a non-empty key is provided, verify it before saving
    if key:
        is_valid, validation_msg = await validate_api_key(provider=provider, api_key=key)
        if not is_valid:
            raise HTTPException(
                status_code=400,
                detail=f"API key verification failed: {validation_msg}",
            )

    try:
        from artemis.config import settings

        settings.set_api_key(provider, key, persist_to_env=request.persist_to_env)

        # Re-run all diagnostic probes to build updated report
        readiness_engine.invalidate_cache()
        updated_report = await readiness_engine.run_all(force_refresh=True)
        action_desc = (
            "successfully verified, updated, and applied" if key else "successfully cleared"
        )
        return {
            "status": "success",
            "message": f"API key for {provider} {action_desc}.",
            "provider": provider,
            "report": updated_report,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to update credentials: {exc}")


@router.get("/model-config-env")
async def get_model_config_and_env():
    """Retrieve the current active artemis.jsonc configuration and .env status for custom setup."""
    import os
    from artemis.config.paths import ROOT_DIR, get_config_path
    from artemis.config import settings
    from artemis.utils.file import load_jsonc

    from artemis.config.settings import is_placeholder_key

    # 1. Config file resolution
    config_path = None
    config_content = ""
    parsed_config = {}
    try:
        config_path_obj = get_config_path("artemis.jsonc")
        config_path = str(config_path_obj)
        config_content = config_path_obj.read_text(encoding="utf-8")
        with open(config_path_obj, encoding="utf-8") as f:
            parsed_config = load_jsonc(f)
    except Exception as e:
        config_content = f"// Error reading config: {e}"

    # 2. Env file resolution
    env_path = ROOT_DIR / ".env"

    # 3. Relevant Env Variables status
    def get_real_env_value(key_name: str, provider: str) -> str | None:
        k = settings.get_api_key(provider)
        if k and not is_placeholder_key(k):
            return k.get_secret_value()
        val = os.environ.get(key_name)
        if val and val.strip() and not is_placeholder_key(val):
            return val.strip()
        return None

    def mask_key(k: str | None) -> str | None:
        # Expose only enough to recognize which key is active, never a usable
        # fragment of the secret itself.
        if not k:
            return None
        if len(k) <= 8:
            return "****"
        return f"****{k[-4:]}"

    gemini_real = get_real_env_value("GEMINI_API_KEY", "google") or get_real_env_value(
        "GOOGLE_API_KEY", "google"
    )
    openai_real = get_real_env_value("OPENAI_API_KEY", "openai")
    anthropic_real = get_real_env_value("ANTHROPIC_API_KEY", "anthropic")
    openrouter_real = get_real_env_value("OPEN_ROUTER_API_KEY", "openrouter")
    xai_real = get_real_env_value("XAI_API_KEY", "xai")
    base_url_val = settings.OPENAI_BASE_URL or os.environ.get("OPENAI_BASE_URL")
    if base_url_val and is_placeholder_key(base_url_val):
        base_url_val = None
    ocr_real = get_real_env_value("OCR_API_KEY", "ocr") or get_real_env_value(
        "VISION_API_KEY", "ocr"
    )

    env_vars = [
        {
            "name": "GEMINI_API_KEY",
            "provider": "google",
            "is_set": bool(gemini_real),
            "preview": mask_key(gemini_real),
            "description": "Google Gemini multimodal vision API key (free tier available)",
        },
        {
            "name": "OPENAI_API_KEY",
            "provider": "openai",
            "is_set": bool(openai_real),
            "preview": mask_key(openai_real),
            "description": "OpenAI API key (GPT-4o, GPT-4o-mini)",
        },
        {
            "name": "ANTHROPIC_API_KEY",
            "provider": "anthropic",
            "is_set": bool(anthropic_real),
            "preview": mask_key(anthropic_real),
            "description": "Anthropic Claude API key (Claude 3.5 Sonnet, Claude 3.7)",
        },
        {
            "name": "OPEN_ROUTER_API_KEY",
            "provider": "openrouter",
            "is_set": bool(openrouter_real),
            "preview": mask_key(openrouter_real),
            "description": "OpenRouter unified API gateway key",
        },
        {
            "name": "XAI_API_KEY",
            "provider": "xai",
            "is_set": bool(xai_real),
            "preview": mask_key(xai_real),
            "description": "xAI Grok vision API key",
        },
        {
            "name": "OPENAI_BASE_URL",
            "provider": "custom",
            "is_set": bool(base_url_val),
            "preview": base_url_val,
            "description": "Custom API endpoint (for local Ollama, vLLM, DeepSeek, or proxies)",
        },
        {
            "name": "VISION_API_KEY",
            "provider": "ocr",
            "is_set": bool(ocr_real),
            "preview": mask_key(ocr_real),
            "description": "Google Cloud Vision OCR key for screen text detection (optional)",
        },
    ]

    return {
        "config_path": config_path or "config/artemis.jsonc",
        "config_filename": "artemis.jsonc",
        "config_content": config_content,
        "default_model": parsed_config.get("default", {}),
        "presets": parsed_config.get("presets", {}),
        "env_path": str(env_path),
        "env_filename": ".env",
        "env_vars": env_vars,
    }


@router.get("/server-status")
async def get_server_runtime_status():
    """Retrieve runtime status, PID, port, and uptime of the Artemis server."""
    import os
    from artemis.runtime.server_lifecycle import get_server_status

    try:
        from apps.admin_console.core.state import state
    except ImportError:
        from admin_console.core.state import state

    port = getattr(state, "port", 8000)
    status = get_server_status(port=port)
    # Explicit DTO: the raw metadata file additionally holds the lifecycle
    # token, cmdline, and filesystem paths, none of which belong on the wire.
    return {
        "running": status["running"],
        "port": status["port"],
        "pids": status["pids"],
        "active_pid": status["active_pid"],
        "uptime_seconds": status["uptime_seconds"],
        "url": status["url"],
        "admin_url": status["admin_url"],
        "current_pid": os.getpid(),
    }


@router.post("/restart")
async def restart_server_endpoint(request: Request):
    """Request a graceful restart of the Artemis server from thin clients/UI."""
    import asyncio
    import os
    import sys
    import threading

    _require_loopback_request(request, "Server lifecycle controls are local-only.")

    try:
        from apps.admin_console.core.state import state
    except ImportError:
        from admin_console.core.state import state

    port = getattr(state, "port", 8000)
    current_pid = os.getpid()

    def _restart_worker():
        import time

        time.sleep(0.6)
        if sys.platform != "win32":
            try:
                os.execv(sys.executable, [sys.executable] + sys.argv)
            except Exception:
                import subprocess

                subprocess.Popen([sys.executable] + sys.argv)
                os._exit(0)
        else:
            import subprocess

            subprocess.Popen([sys.executable] + sys.argv)
            os._exit(0)

    threading.Thread(target=_restart_worker, daemon=True).start()

    return {
        "status": "restarting",
        "message": "Artemis server is restarting. Client reconnection should occur in 2-3 seconds.",
        "previous_pid": current_pid,
        "port": port,
    }


@router.post("/shutdown", status_code=202)
async def shutdown_server_endpoint(request: Request):
    """Request a graceful shutdown of the Artemis server."""
    import asyncio

    try:
        from apps.admin_console.core.state import state
    except ImportError:
        from admin_console.core.state import state

    _require_local_lifecycle_request(request)
    server = getattr(request.app.state, "uvicorn_server", None)
    if server is None:
        raise HTTPException(status_code=503, detail="Server lifecycle controller is unavailable.")

    async def _shutdown_after_response() -> None:
        # Let Starlette flush the accepted response before Uvicorn leaves its
        # main loop and invokes the FastAPI shutdown lifecycle.
        await asyncio.sleep(0.05)
        state.is_shutting_down = True
        state.shutdown_event.set()
        server.should_exit = True

    asyncio.create_task(_shutdown_after_response())

    return {
        "status": "shutting_down",
        "message": "Artemis server is shutting down.",
        "pid": os.getpid(),
    }
