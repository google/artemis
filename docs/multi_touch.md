# Advanced gesture input

`perform_gesture` adds continuous and multi-finger input alongside the existing
`click` and `swipe` primitives. Use it when a normal swipe cannot express the
interaction. It uses the existing Operator, Validator, MCP and Android input path.

Coordinates are integers from **0 to 1000**, relative to the current screen. Each
example below is a direct MCP call. Operator and Flash calls also require
`target_description`, describing the intended target and purpose.

## Multi-touch

All pointers in a phase start together. This example pinches inward; reverse the
paths to zoom outward. Add another pointer for a three-finger gesture.

```json
{
  "action": "perform_gesture",
  "phases": [{
    "duration_ms": 800,
    "pointers": [
      {"id": 0, "path": [[300, 500], [450, 500]]},
      {"id": 1, "path": [[700, 500], [550, 500]]}
    ]
  }]
}
```

## Long press and drag

Use `long_press_drag` to hold an object, move it to a chosen endpoint, optionally
hold there, then release. **The endpoint can be anywhere on screen.**

```json
{
  "action": "perform_gesture",
  "phases": [{
    "kind": "long_press_drag",
    "start": [300, 400],
    "end": [650, 550],
    "hold_ms": 700,
    "duration_ms": 800,
    "release_delay_ms": 500
  }]
}
```

- `hold_ms`: initial long press, 1–5000 ms. If omitted, uses the device's configured
  long-press timeout plus 150 ms.
- `duration_ms`: movement time, 1–5000 ms; default 800.
- `release_delay_ms`: time to stay pressed at `end`, 0–5000 ms; default 0. Zero
  releases as soon as movement completes. A positive delay maintains contact
  before release.

For an edge dwell, choose an endpoint near the desired edge, such as
`"end": [980, 400]`, with a positive `release_delay_ms`. This is the same generic
drag operation. The target UI determines its edge activation region; inspect the
result afterward.

### Curved drag

Add two `control_points` for a cubic Bézier curve between the chosen `start` and
`end`. They shape the path; they do not replace or constrain its endpoint.

```json
{
  "action": "perform_gesture",
  "phases": [{
    "kind": "long_press_drag",
    "start": [300, 400],
    "end": [650, 550],
    "control_points": [[400, 250], [600, 250]],
    "duration_ms": 800,
    "release_delay_ms": 500
  }]
}
```

Omit `control_points` for a straight path. The helper constructs the curve from
the supplied points. It never redirects the endpoint to an edge.

## Explicit continuous phases

For multiple pointers or intermediate waypoints, use explicit phases. Keep the
same pointer IDs across phases and start each path at its previous endpoint.
Contacts stay down between phases and lift after the final phase. A one-point
path holds still, allowing one pointer to hold while another moves.

```json
{
  "action": "perform_gesture",
  "phases": [
    {"duration_ms": 700, "pointers": [{"id": 0, "path": [[300, 400]]}]},
    {"duration_ms": 800, "pointers": [{"id": 0, "path": [[300, 400], [650, 550]]}]},
    {"duration_ms": 500, "pointers": [{"id": 0, "path": [[650, 550]]}]}
  ]
}
```

An explicit pointer path may also contain `control_points: [[c1x,c1y],[c2x,c2y]]`;
then `path` must contain exactly the start and end. Do not mix a `long_press_drag`
entry with explicit phases in one call.

Limits: 1–10 pointers, 1–32 phases, 1–128 path points per pointer, 1–5000 ms per
phase and at most 30000 ms total. Coordinates and controls must stay within
0–1000. Pointer IDs are unique integers from 0 to 9. Invalid plans are rejected
before input starts.

## Compatibility and results

The bundled helper is **v1.3.3 / version code 10**. Single-phase gestures support
Android 7+; continuous phases and `long_press_drag` require Android 8+.
Unsupported helpers return `UNSUPPORTED`. Multi-touch and continuous gestures
are never emulated with separate swipes. Ship the APK and its matching
`helper_manifest.json` together.

Results use the existing `ActionResult` (`ok`, `code`, `message`). Success means
input completed; verify the intended UI effect afterward. A timeout does not
prove the input was never dispatched, so `perform_gesture` is not automatically
retried. Existing actions keep their retry and history behavior.

Cancellation requests release of held contacts after the current bounded phase.
If release cannot be confirmed, the helper blocks further gesture injection until
recovery. This helper protection does not cover raw ADB input.
