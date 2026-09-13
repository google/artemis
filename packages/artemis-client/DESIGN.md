# Artemis Thin Client Design

## Goals

Developer machines install only a small remote SDK, while the host connected to
Android devices runs the full Artemis stack. The SDK must:

- have no dependency on ADB, device drivers, agents, LLMs, web services, or image libraries;
- never attempt to start a local Artemis daemon;
- remain compatible with the existing server API;
- allow the server to add agent and device capabilities independently; and
- provide explicit capability negotiation and errors as the protocol evolves.

## Boundary

The client owns request modeling, HTTP communication, status polling, error
normalization, and result models. Device discovery, task scheduling, concurrency
control, model selection, execution, logs, videos, and artifact generation all
belong to the server.

The distribution is named `artemis-client`, while the Python import is
`artemis_client`. This prevents it from shadowing the full runtime's top-level
`artemis` package. The full runtime can depend on this package later and
re-export the client from `artemis` for compatibility.

## Phase 1: Compatibility Adapter

The first release calls the current `/api/*` administration endpoints directly.
The client generates a task ID and maps it to the server's existing `session_id`
to make retries idempotent. While a task is queued but not yet visible in the
session database, the client reconstructs its state from `queue` and
`active_tasks` in `/api/status`.

Experimental parameters live in `options`. Older servers ignore unknown fields,
while newer servers can consume them without requiring an immediate SDK release.

## Phase 2: Stable Remote API

The server should expose versioned endpoints dedicated to remote clients:

```text
POST   /api/v1/tasks
GET    /api/v1/tasks/{task_id}
DELETE /api/v1/tasks/{task_id}
GET    /api/v1/tasks/{task_id}/events
GET    /api/v1/tasks/{task_id}/artifacts
GET    /api/v1/devices
GET    /api/v1/capabilities
```

The administration API and remote SDK API should remain separate. The remote API
uses a consistent error envelope:

```json
{
  "error": {
    "code": "DEVICE_OFFLINE",
    "message": "The selected device is offline",
    "retryable": false
  }
}
```

## Compatibility Rules

1. `/api/v1` may add optional request and response fields, but must not change existing field semantics.
2. The client ignores unknown response fields and preserves the original response in each model's `raw` attribute.
3. Every new request field must have a server-side default.
4. The SDK checks `/api/v1/capabilities` before using a new feature.
5. Breaking changes ship under `/api/v2`; they are never applied in place.
6. The SDK and server follow semantic versioning independently.

## Security

The current daemon exposes administration and credential-related endpoints and
must not listen on a public interface without protection. Use an SSH or VPN tunnel
in the short term. In production, place a reverse proxy in front of the daemon,
expose only the remote API, and enforce HTTPS, bearer-token authentication,
request-size limits, rate limits, and audit logging. Tokens are accepted only
through arguments or environment variables and are never persisted by the SDK.

## Future Work

- SSE event subscriptions with reconnection support;
- artifact manifests and streaming downloads;
- batch task submission;
- server and API version constraints;
- retries limited to network failures and idempotent requests; and
- OpenAPI contract tests to prevent server and SDK drift.
