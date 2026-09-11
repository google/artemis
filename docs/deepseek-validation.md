# DeepSeek validation record

Tested on 2026-09-12 (UTC+08), Windows, Python 3.12, a physical Android 16
device with Accessibility Helper 1.2.0, and `deepseek-flash` at
`https://api.deepseek.com`. Base revision: `086078819209c7139d6f833cfdc6d5cc80d9f19a`.
The requests described below used the live service. Offline tests are listed
separately. This is a bounded compatibility check, not a reliability benchmark.

## Reproduce the original API errors

Set `DEEPSEEK_API_KEY` and run this script with the project's dependencies.
It deliberately uses the pre-fix OpenAI path, including explicit endpoint
credentials, so it remains a reproducible comparison after adding the provider.

```python
import asyncio
import os

from langchain_core.messages import HumanMessage

from artemis.agents.hopper.hopper import HopperOutput
from artemis.llm.router import ModelEndpoint, ModelFactory, ModelProvider


async def main():
    model = ModelFactory.create_model(
        ModelEndpoint(
            provider=ModelProvider.OPENAI,
            model_name="deepseek-flash",
            api_key=os.environ["DEEPSEEK_API_KEY"],
            api_base="https://api.deepseek.com",
            max_tokens=512,
            timeout_seconds=20,
        )
    )
    prompt = [
        HumanMessage(
            content=(
                "Find Settings in this list: com.android.settings. "
                "Return found=true, output=com.android.settings, and a brief reason."
            )
        )
    ]
    cases = [
        ("default", {}, {}),
        ("function_calling", {"method": "function_calling"}, {}),
        (
            "function_calling_nonthinking",
            {"method": "function_calling"},
            {"extra_body": {"thinking": {"type": "disabled"}}},
        ),
    ]
    for name, schema_options, request_options in cases:
        try:
            result = await model.with_structured_output(HopperOutput, **schema_options).ainvoke(
                prompt, **request_options
            )
            print(name, result.model_dump())
        except Exception as error:
            # Sanitize credentials even when a client includes them in an error.
            message = str(error).replace(os.environ["DEEPSEEK_API_KEY"], "[REDACTED]")
            print(name, type(error).__name__, message)


asyncio.run(main())
```

Sanitized output (unrelated fields omitted):

```text
default: HTTP 400 / This response_format type is unavailable now
function_calling: HTTP 400 / Thinking mode does not support this tool_choice
function_calling_nonthinking: found=true, output=com.android.settings
```

The first failure comes from ChatOpenAI's default schema format. Changing only
the schema method reveals the second failure: DeepSeek's default thinking mode
rejects forced tool selection. Both request changes are necessary. The dedicated
provider applies them through the model factory and structured-output wrapper.

## Phone task: before and after

Use the configuration in [DeepSeek configuration](deepseek.md), an unlocked
device, and the SDK setup below. For the baseline, the identical config used
`provider: "openai"` in both default and fallback, with `OPENAI_BASE_URL` set to
the DeepSeek URL and `OPENAI_API_KEY` set from the DeepSeek credential. The fixed
run uses the dedicated key and provider instead. Do not put keys in source files.

```python
import asyncio

from artemis import Agent, Builders
from artemis.config import initialize_llm_config
from artemis.sdk.types.task import AgentProfile


async def main():
    profile = AgentProfile(name="default", llm_config=initialize_llm_config())
    config = (
        Builders.AgentConfig.with_default_profile(profile)
        .with_video_recording_tools(False)
        .with_flash_config(max_turns=8, step_summarizer=False, explorer_mode="pro")
        .build()
    )
    agent = Agent(config=config)  # Connect one authorized device.
    try:
        await asyncio.wait_for(agent.init(retry_count=1, retry_wait_seconds=1), 60)
        request = (
            agent.new_task(
                "Open Android battery settings and read the current battery percentage. "
                "Do not change any settings, open accounts, or send messages. "
                "Verify the visible percentage before reporting it."
            )
            .using_profile("flash")
            .with_max_steps(8)
            .build()
        )
        print(await asyncio.wait_for(agent.run_task(request=request), 180))
    finally:
        await agent.clean()


asyncio.run(main())
```

Sanitized log excerpts and outcome summaries; identifiers, local paths,
screenshots, and UI trees are omitted. The localized app name is translated.

```text
BEFORE: OpenAI alias pointed at DeepSeek
Flash Turn 1/8: manage_app(action=launch, app_name=Settings)
Starting Hopper Agent
HTTP 400: This response_format type is unavailable now
Retries exhausted; execution paused; bounded SDK run timed out.

AFTER: dedicated DeepSeek provider, device ready
Turn 1/8: manage_app(action=launch, app_name=Settings)
Starting Hopper Agent
POST https://api.deepseek.com/chat/completions -> 200 OK
App com.android.settings is ready
Turn 2/8: click(target=10)
Turn 3/8: swipe(direction=up)
Turn 4/8: click(target=23)
Turn 5/8: report_task_status(status=completed)
Final result: completed; battery percentage 87%.
```

The final battery page was inspected locally and showed 87%, matching the
reported result. This exercises real model calls, Hopper parsing, app launch,
device actions, and a final status report. Phone screenshots and raw traces
are not attached.

An earlier post-fix attempt also returned successful model responses but ended
at the eight-turn limit while the notification shade was visible and app launch
reported no focused package. It is a failed run, not evidence of end-to-end
success. After confirming the device was unlocked and returning to the launcher,
the same task completed as above. This provider change does not fix Android
foreground detection, retry classification, or device readiness.

## Deterministic and quality checks

| Check | Base | With this change |
| --- | --- | --- |
| Full default pytest selection | 2,109 passed, 82 failed | 2,117 passed, 82 failed |
| Skipped / deselected | 4 / 8 | 4 / 8 |
| DeepSeek and existing LLM regression selection | — | 40 passed (8 new) |
| Ruff format | — | Passed, 619 files |
| Ruff check, entire repository | 11 findings | Same 11 findings |
| Ruff check, changed Python files | — | Passed |
| Protected-core Pyright | — | 0 errors / warnings |
| Quality ratchet | — | Passed: 754 broad handlers, 0 silent handlers, 18 type ignores |

The full-suite failed test IDs are identical between clean base and the changed
worktree. The complete Ruff outputs are also identical. The full suite is not
green; no claim is made that this patch fixes those existing failures.

Both full-suite runs used the same local-only pytest fixture that replaces
`mcp_server.tools.task_runner._start_spawn_watchdog` with a no-op **only** in
`test_mcp_tools.py`. This prevents the known mocked-process watchdog leak
reported in [#43](https://github.com/google/artemis/issues/43) from emitting delayed desktop notifications. No model calls or
assertions were bypassed by that fixture. The fixture was outside the repository
and is not part of this patch; focused LLM tests ran without it.

The live scope is the short Flash configuration above. Pro orchestration,
Gemini-native grounding, video analysis, and long-history summarization were
not validated with DeepSeek. See the configuration document for these limits.
