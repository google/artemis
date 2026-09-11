# DeepSeek configuration

Set `DEEPSEEK_API_KEY` in the environment before starting ARTEMIS. Select
`provider: "deepseek"` and a model available to your account. This provider uses
`https://api.deepseek.com` through the existing `langchain-openai` dependency;
it does not reuse `OPENAI_API_KEY` or `OPENAI_BASE_URL`.

For a short Flash task, save the following as a JSONC file and set
`ARTEMIS_ARTEMIS_JSONC` to its absolute path before starting the process:

```jsonc
{
  "default": {
    "provider": "deepseek",
    "model": "deepseek-flash",
    "temperature": 0.0,
    "fallback": {"provider": "deepseek", "model": "deepseek-flash"}
  },
  "agent": {
    "flash": {"max_turns": 8, "step_summarizer": {"enabled": false}},
    "explorer": {"flash_mode": "pro"},
    "video_analyzer": {"enabled": false}
  }
}
```

The fallback uses the same model here to keep the example on one provider;
it is not an independent recovery endpoint. Keep credentials out of this file.
The Android device must be connected, authorized, unlocked, and have the
Accessibility Helper enabled.

## Why a dedicated provider is needed

Pointing the OpenAI provider at DeepSeek is sufficient for ordinary chat, but
ARTEMIS also requests typed results through `with_structured_output`, including
Hopper's app selection. Two differences were reproduced with `deepseek-flash`:

| Request | Observed result |
| --- | --- |
| Default ChatOpenAI structured output (`json_schema`) | HTTP 400: `This response_format type is unavailable now` |
| `method="function_calling"` with default thinking | HTTP 400: `Thinking mode does not support this tool_choice` |
| Function calling with `thinking.type="disabled"` | Typed Hopper output parsed successfully |

The DeepSeek factory therefore disables thinking for its requests, and the
ARTEMIS structured-output wrapper defaults to `method="function_calling"` for
this provider. An explicitly supplied method is preserved. OpenAI and other
providers retain their existing defaults. This integration does not offer a
thinking-mode switch because ARTEMIS's forced schema calls require this mode.

## Scope and verification

See the [validation record](deepseek-validation.md) for runnable reproduction
scripts, sanitized before/after logs, phone outcomes, and baseline test results.

The example intentionally disables the screenshot step summarizer and video
analyzer and selects Explorer's generic `pro` grounding mode. It does not establish
compatibility with Gemini-native grounding, video utilities, long-running
history compression, or the complete Pro execution profile. Choosing an image-capable
model is required for screenshot-based tasks; text-only API compatibility is
insufficient. Model availability and supported inputs can change; consult the
[DeepSeek API documentation](https://api-docs.deepseek.com/) and
[vision documentation](https://api-docs.deepseek.com/guides/vision/).

Offline regression coverage is in `tests/unit/test_deepseek_provider.py`:
configuration and dedicated-key validation, endpoint overrides, secret handling,
unchanged defaults for other providers, and an intercepted HTTP request that
checks authentication, the non-thinking setting, forced function output, and
Pydantic parsing. Run it with:

```sh
uv run pytest -q tests/unit/test_deepseek_provider.py
```

Live validation requires a real API key and an authorized Android device and
is not part of the deterministic suite. Keep phone screenshots, UI trees,
identifiers, and raw traces local; publish only sanitized textual results.
