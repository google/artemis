# LLM gateways and per-node endpoints

The `provider` selects the API protocol. The `model` can be a route alias
configured on a compatible gateway. Keep credentials in `.env` or the MCP
server environment, and restart Artemis after changing environment settings.

## Anthropic-compatible gateway with Bearer authentication

Set these values in `.env`, replacing the example host and token:

```dotenv
ANTHROPIC_BASE_URL=https://gateway.example.com
ANTHROPIC_AUTH_TOKEN=your_gateway_token_here
```

Merge the top-level `default` and `nodes` settings from
[`anthropic-gateway.jsonc`](../config/examples/anthropic-gateway.jsonc) into
`config/artemis.jsonc`. Retain your other application settings and replace the
model aliases with routes available on your gateway.

`ANTHROPIC_AUTH_TOKEN` sends `Authorization: Bearer ...` without an `x-api-key`
header. It takes precedence over the global `ANTHROPIC_API_KEY`. For the
official API with API-key authentication, leave the token and base URL unset
and configure `ANTHROPIC_API_KEY`. An explicit SDK `ModelEndpoint.api_key`
takes precedence over the global token.

## Different endpoints for different nodes

[`multi-endpoint.jsonc`](../config/examples/multi-endpoint.jsonc) demonstrates:

- The default OpenAI-compatible model uses `primary.example.com`.
- Its fallback uses `backup.example.com`.
- The operator uses `vision.example.com` and keeps the default fallback.
- The outputter switches to Anthropic for both primary and fallback. It drops
  the inherited OpenAI URLs and uses `ANTHROPIC_BASE_URL` from `.env`.

Configure `OPENAI_API_KEY` for the OpenAI-compatible endpoints and the
Anthropic variables above for the outputter. These examples use one shared
credential per provider; each server using that provider must accept it.
Per-node URLs do not provide separate credentials or authentication modes.

For OpenAI and Anthropic, URL precedence is:
`api_base` on the node → provider environment/settings URL → provider default.
OpenAI-compatible base URLs normally include `/v1`; the Anthropic SDK appends
`/v1/messages`, so its base URL should omit `/v1`. Gateway path prefixes are
preserved, for example `https://gateway.example.com/anthropic`.

Changing only a model preserves its inherited URL. Changing provider removes
an inherited URL unless the override explicitly supplies `api_base`. The same
rule applies to fallbacks and SDK profile overrides. `api_base: null` clears
the inherited URL and falls back to environment/settings; it does not bypass
a configured global gateway. To choose the official service explicitly, set
its full base URL and ensure the provider credential is valid there.

The examples above use the unified `default`/`nodes` format. SDK
`AgentProfile(from_file=...)` files instead contain expanded node overrides, such as
`{"planner": {"provider": "openai", "model": "gateway-primary"}}`.

`mobile_diagnose(verify_credentials=true)` checks the configured global
Anthropic credential at `ANTHROPIC_BASE_URL` with its selected authentication
method. It does not validate every per-node `api_base`. The credential check
uses `/v1/models`; a gateway exposing only `/v1/messages` may reject this probe
even when inference works.
