<!--
Copyright 2026 Google LLC

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
-->

# On-Device Lightweight VLM Support — Design

Status: Draft
Tracks: roadmap item **On-Device Lightweight VLMs** (`README.md`); see google/artemis issue #131.

## 1. Goal

Run ARTEMIS perception and control loops against **local, lightweight vision-language
models** (3B–8B class, e.g. `qwen2.5vl:7b`) instead of — or alongside — cloud APIs,
for low latency, offline operation, and privacy-first automation.

This document describes the endpoint plumbing proposed in issue #131 and the design
for what comes next.

## 2. Deployment Shapes

All shapes expose an **OpenAI-compatible HTTP API**; ARTEMIS never talks to model
runtimes directly. The only difference is where the server lives.

| Shape | Server | `api_base` example | Notes |
|---|---|---|---|
| Host-side | Ollama on the dev machine | `http://localhost:11434/v1` | Zero-config default; the `local-ollama` preset in `config/artemis.jsonc` |
| Host-side | llama.cpp `llama-server` | `http://localhost:8080/v1` | GGUF quantized VLMs; provider `custom` |
| Host-side | vLLM | `http://localhost:8000/v1` | GPU hosts; provider `vllm` |
| LAN edge box | Ollama/vLLM on a home server | `http://192.168.1.10:8000/v1` | Shares one GPU across workstations; set `api_key` if the box is shared |
| On-SoC (future) | NPU/GPU runtime on the phone itself, fronted by a localhost shim app | `http://127.0.0.1:<port>/v1` | Requires the perception-pipeline work in §4 (small context, tight latency budget) |

## 3. Endpoint Configuration Schema

Endpoint knobs now live on the `LLM` / `LLMWithFallback` config schema
(`artemis/config/llm.py`) and are forwarded to the router's `ModelEndpoint`
(`artemis/llm/router.py`) by `_resolve_endpoint()` (`artemis/services/llm.py`),
for primary **and** fallback nodes alike:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `provider` | string | — | `ollama`, `vllm`, `custom`, plus the existing cloud providers. Aliases are normalized by `ModelProvider.from_string`. |
| `model` | string | — | Model identifier as the server knows it (e.g. `qwen2.5vl:7b`). |
| `api_base` | string \| null | env `OPENAI_BASE_URL`, else `http://localhost:8000/v1` | OpenAI-compatible base URL. |
| `api_key` | string \| null | `"EMPTY"` for local providers | Only needed for shared/authenticated servers. |
| `max_tokens` | int \| null | server default | Completion cap; keep small for on-device models. |
| `timeout_seconds` | float \| null | `60.0` | Per-request timeout; raise for slow quantized models. |
| `is_multimodal` | bool \| null | `true` | Whether the endpoint accepts image inputs; set `false` for text-only local models so perception nodes are not routed to them. |

`LLM.validate_provider()` no longer demands cloud API keys for `ollama` / `vllm` /
`custom` — connectivity is a runtime property of `api_base`, not a credential.

### Example: hybrid local-flash + cloud-pro setup

```jsonc
// config/artemis.jsonc
{
  // Cheap, private, low-latency default tier served by Ollama.
  "default": {
    "provider": "ollama",
    "model": "qwen2.5vl:7b",
    "api_base": "http://localhost:11434/v1",
    "max_tokens": 2048,
    "timeout_seconds": 120,
    "fallback": {
      // Cloud safety net when the local server is down or unsure.
      "provider": "google",
      "model": "gemini-3.8-flash"
    }
  },
  "nodes": {
    // Keep the hard reasoning on a cloud pro model.
    "planner": {
      "provider": "google",
      "model": "gemini-3.8-pro",
      "fallback": { "provider": "ollama", "model": "qwen2.5vl:7b" }
    },
    // Coordinate grounding stays on a specialized ER model for now (see §4).
    "object_detector": {
      "provider": "google",
      "model": "gemini-robotics-er-2-preview"
    },
    // High-frequency lightweight judges are ideal for the local tier.
    "hopper": {
      "provider": "ollama",
      "model": "qwen2.5vl:3b",
      "api_base": "http://localhost:11434/v1"
    }
  }
}
```

A ready-made starting point ships as the `local-ollama` preset in
`config/artemis.jsonc`; environment fallbacks are documented in `.env.example`
(`OPENAI_BASE_URL`, commented).

## 4. Perception Pipeline — Next Steps

Endpoint plumbing alone does not make a 7B VLM a good UI agent. The following
work items close the gap (tracked as separate issues):

- **Screenshot resize policy.** Local VLMs have small effective vision
  resolutions and token budgets. Add a deterministic resize/tile stage before
  the screenshot enters the prompt: cap the long edge, keep the aspect ratio,
  and record the scale factor alongside the image so coordinates can be mapped
  back. Candidate home: the screenshot acquisition path in
  `artemis/drivers/` / the Explorer input builders in `artemis/agents/`.
- **Coordinate adapter.** Every `[x, y]` the model emits must be scaled
  back through the recorded factor before hitting the controller
  (`artemis/controllers/`). Until this lands, keep `object_detector` /
  `explorer` on cloud ER models (they are fine-tuned for sub-pixel grounding;
  see the `object_detector` note in `config/artemis.jsonc`).
- **Small-context memory.** The default transcript budget
  (`agent.memory.transcript.context_budget_tokens`, currently tuned for
  1M-token cloud contexts) must scale down for 8k–32k local contexts: lower
  `start_ratio`/`soft_ratio`, lean harder on `image_scrub_depth` and the
  chunking/recall layers so history fits.
- **Structured-output discipline.** Small models degrade on long tool
  schemas. Prefer the structured-output path (`artemis/llm/structured.py`)
  with minimal schemas per node, and disable `include_thoughts`-style
  reasoning traces the endpoint cannot honor (`reasoning_effort` is already
  forwarded for vLLM/custom endpoints).
- **Capability gating.** Use `is_multimodal: false` to keep text-only
  local models away from perception nodes; `_resolve_endpoint` already carries
  the flag to `ModelEndpoint`.

## 5. Privacy Model

- With a fully local tier, **screenshots, UI hierarchies, and task text never
  leave the machine**; the LAN edge-box shape extends the trust boundary to the
  local network only.
- Hybrid setups leak data by design at the fallback boundary. Rules of thumb:
  - Fallbacks to cloud providers should be opt-in per node for
    privacy-sensitive tasks; set the fallback to another local endpoint to stay
    fully offline.
  - `api_key` values belong in `.env` or a secrets manager, not in committed
    config files. `ModelEndpoint.cache_key()` already hashes the key, so keys
    never appear in cache indexes or logs.
  - Telemetry (`artemis/telemetry/`) must not include prompt or image payloads
    for local endpoints; audit before enabling.

## 6. Latency Measurement Plan

On-device VLMs only pay off if step latency beats the cloud path. Measure, don't
assume:

1. **Instrumentation.** Reuse the existing per-call accounting in
   `artemis/services/token_meter.py` (prompt sizes, cache-hit ratios) and the
   trace pipeline (`artemis/data_engine/trace.py`) to tag every call with
   provider, model, and `api_base`, so local vs. cloud turns are separable in
   the same session.
2. **Metrics.** Per node: time-to-first-token, total step latency (observe →
   think → act), tokens/sec, and fallback rate (how often the local tier gave
   up and the cloud fallback fired — via the circuit-breaker/fallback counters
   in `artemis/llm/reliability.py`).
3. **Benchmark harness.** Run a fixed task suite in Flash profile against (a)
   cloud default, (b) `local-ollama` preset, (c) hybrid config from §3, on the
   same device. Success bar for the roadmap item: median step latency of the
   local tier ≤ cloud tier on routine tasks, with task success within an agreed
   delta.
4. **Gates.** CI keeps provider-surface coverage via
   `tests/unit/test_llm_router.py`; device-level latency benchmarks run
   manually (marked `android`/`manual`) rather than in CI.
