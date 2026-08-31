# ARTEMIS Video Analysis Memory and Reliability Design

Status: implemented (2026-08-25)  
Audience: ARTEMIS runtime, perception, tracing, and mobile automation maintainers

## 1. Executive summary

The current video analyzer treats its blackboard as state owned by one
`VideoAnalyzer` object. Tool invocations create new analyzer objects, so the
state is lost between calls. A failed parent analysis also discards useful
work completed by child analyzers. Overlap detection is advisory and does not
prevent the same interval from being sent to a video model again.

This design moves video memory out of the agent and into a video-scoped,
persistent blackboard. The blackboard is the source of truth for:

- which intervals have been analyzed for a normalized query and modality;
- observations and evidence extracted from those intervals;
- successful, running, and failed segment attempts;
- resumable partial results across tool calls;
- concurrency leases that prevent duplicate model requests.

The analyzer becomes a coordinator over this state. It calculates the set
difference between a requested interval and completed coverage, analyzes only
the gaps, commits every successful chunk immediately, and returns partial
results without erasing prior work.

The first implementation deliberately uses SQLite and the existing trace
directory. Vector search, semantic graphs, provider-managed context caches,
and entity re-identification are future layers, not prerequisites for fixing
correctness and reliability.

## 2. Goals

### 2.1 Functional goals

1. Share analyzed intervals and results across every `VideoAnalyzer` created
   for the same recording.
2. Avoid repeating an identical successful `(video, interval, query,
   modality)` analysis.
3. Persist each successful chunk before sibling chunks or the parent agent
   finish.
4. Preserve successful chunks when some chunks fail.
5. Expose previous observations to both native Gemini and universal-model
   coordinators.
6. Keep evidence tied to recording-relative timestamps and durable files.
7. Prevent concurrent duplicate requests with an expiring lease.
8. Maintain existing public tool signatures.

### 2.2 Reliability goals

- A process or model failure may lose only the currently running chunk.
- An expired running lease is reclaimable.
- Retrying a request analyzes only uncovered or failed chunks.
- A partial result explicitly identifies failed intervals.
- SQLite writes are idempotent and safe under multiple analyzer instances.
- Evidence used by persistent memory is not deleted by temporary-file cleanup.

### 2.3 Non-goals for phase one

- Semantic similarity reuse between materially different queries.
- Cross-recording memory or user identity memory.
- Replacing Gemini or the configured universal VLM.
- Hosting an open-weight video model or accessing model KV caches.
- Full WorldMM semantic graphs, HippoRAG, VLM2Vec, or ReflectWorld ReID.
- Permanent retention of uploaded provider files.

## 3. Current failure modes

### 3.1 Agent-owned state

`VideoAnalyzer.__init__` initializes an empty list. `video_tool.py` creates a
new analyzer for every tool call. Consequently, a later invocation cannot see
the earlier invocation's ledger.

### 3.2 Advisory overlap handling

The analyzer calculates high-overlap warnings but continues with extraction,
compression, upload, and inference. A warning in a prompt is not an
idempotency mechanism.

### 3.3 Commit-at-the-end behavior

Some event observations are appended during child analysis, but they are only
in memory. If the parent fails or cleanup runs, the trace can contain expensive
model activity with no reusable result.

### 3.4 Mutable recording ambiguity

An open-ended interval is currently cached as `(start, None)`. The meaning of
`None` changes while recording continues, so this cannot be a stable cache
identity.

### 3.5 Disposable evidence

Extracted screenshots live in temporary directories registered for cleanup.
The ledger may therefore reference files that no longer exist.

## 4. Design principles

### 4.1 Memory before reasoning

Perception results are written before the coordinator generates its final
answer. Answer generation reads stored evidence; it is not the owner of that
evidence.

### 4.2 Evidence-backed records

Every observation carries an interval, query, confidence, modality, model
metadata where available, and an optional durable screenshot. Summaries never
stand alone without temporal provenance.

### 4.3 Objective observation versus query result

The design has two logical layers:

- **Observations** describe visible or audible events and can help later
  queries.
- **Segment results** answer one normalized query over one complete interval
  and define exact reusable coverage.

An observation about a button is useful context for many questions, but a
segment result for "did login succeed" must not automatically satisfy a
different query such as "how long did the spinner remain visible".

### 4.4 Immutable interval identity

An analysis segment always has numeric start and end timestamps. Open-ended
requests are resolved to the safe available end before they are claimed or
committed.

### 4.5 At-least-once execution, idempotent commit

Provider requests can be retried. Database constraints and deterministic keys
make commits idempotent. Exact-once provider execution is not promised, but
duplicate execution is strongly suppressed by leases.

## 5. Architecture

```text
video_analyzer tool
        |
        v
VideoAnalyzer coordinator ---------> persistent blackboard ledger
        |                              |              |
        |                              |              +-- observations/evidence
        |                              +-- segment coverage and attempts
        |
        +-- resolve stable numeric interval
        +-- subtract completed coverage
        +-- claim uncovered chunks with leases
        +-- run child analysis
        +-- commit each successful chunk immediately
        +-- mark failed chunks retryable
        +-- synthesize cached + new + partial results
```

The blackboard is acquired from `ArtemisContext`, not constructed as private
agent state. Multiple analyzers in the same context share the object, while
SQLite makes the data available to later instances and process restarts.

## 6. Recording identity

The preferred blackboard key is the active `RecordingSession.video_id`:

```text
video:<uuid>
```

If no active recording is available, the fallback is the DataEngine session:

```text
session:<uuid>
```

The context retains the resolved blackboard instance so analysis after a
recording stops continues to use the same key. Extracted recording artifacts
also carry an explicit source revision:

```text
<video-uuid>:<generation>:<sealed-end>
```

A generation advances whenever the recorder rolls a physical segment. The
recorder tracks the sealed numeric prefix, and every `end=None` analysis is
resolved to the current safe numeric end before it is claimed.

## 7. Persistence model

### 7.1 `video_analysis_segments`

One row represents a complete query-specific analysis of a numeric interval.

| Column | Meaning |
| --- | --- |
| `board_key` | Recording/session namespace |
| `query_key` | SHA-256 of the normalized query |
| `query_text` | Original query for debugging and retrieval |
| `modality` | `video` or `audio` |
| `start_ms`, `end_ms` | Stable integer interval |
| `status` | `running`, `succeeded`, `retryable_failed`, `permanent_failed` |
| `lease_owner`, `lease_expires_at` | Duplicate-work suppression |
| `attempt_count` | Number of acquired attempts |
| `summary`, `analysis` | Reusable completed result |
| `error` | Last structured failure text |
| `created_at`, `updated_at` | Audit timestamps |

The unique key is:

```text
(board_key, query_key, modality, start_ms, end_ms)
```

### 7.2 `video_analysis_observations`

One row represents a timestamped event or fact.

| Column | Meaning |
| --- | --- |
| `observation_id` | Deterministic content fingerprint |
| `board_key` | Recording/session namespace |
| `start_ms`, `end_ms` | Event interval |
| `query_text` | Query that caused extraction |
| `summary` | Concise observation |
| `confidence` | Model-reported confidence |
| `modality` | `video` or `audio` |
| `screenshot_path` | Durable evidence path |
| `extra_json` | Forward-compatible metadata |
| `created_at` | Audit timestamp |

The deterministic observation ID makes repeated commits safe.

### 7.3 Evidence layout

```text
<trace-session>/video_blackboard/evidence/<content-id>.jpg
```

Temporary screenshots are copied into this directory before their source is
registered for deletion. Stored records reference the durable copy.

## 8. Segment state machine

```text
                  lease expires
running ------------------------------+
  |                                   |
  | success                           v
  +----------> succeeded         retryable_failed
  |                                   |
  | retryable error                   | claim again
  +----------> retryable_failed ------+
  |
  | non-retryable/budget exhausted
  +----------> permanent_failed
```

Claim behavior is transactional:

1. A missing row is inserted as `running`.
2. `succeeded` returns its cached result without acquiring work.
3. `running` with a live lease returns `in_progress`.
4. An expired lease or retryable failure is atomically reclaimed and increments
   `attempt_count`.

Exhausted failures store a normalized category and category-derived
retryability. Permanent failure is reserved for validated bad input,
authentication/policy errors, cancellation, or explicit decisions.

## 9. Coverage and planning

Coverage is query- and modality-specific. Given requested interval `R` and
the union of successful intervals `S`, the work planner calculates:

```text
missing = R - union(S)
```

Each missing interval is split into bounded chunks. Completed chunks are
included in the returned result; only gaps are sent to the provider.

Interval math uses integer milliseconds internally to prevent floating-point
key drift. API-facing timestamps remain seconds.

Observations from other queries are still placed in the ledger as context, but
they do not count as completed coverage for the new query.

## 10. Execution flow

### 10.1 Tool invocation

1. Acquire the context-scoped blackboard.
2. Refresh the local ledger from persistent observations.
3. Give the coordinator the full ledger snapshot.
4. Coordinator requests segment analysis.

### 10.2 Child analysis

1. Resolve a numeric interval from recording metadata.
2. Calculate missing intervals.
3. For every chunk, acquire a lease.
4. Reuse `succeeded`; skip live `running`; execute only `claimed`.
5. Extract/compress/upload and invoke the configured VLM.
6. Persist timeline observations as soon as they are parsed.
7. Persist the complete segment result before returning to the parent.
8. On final failure, mark that exact chunk failed and keep all sibling results.

### 10.3 Parent synthesis

The parent receives cached and newly completed results in the same format. If
some chunks failed, it receives an explicit partial-result marker and failed
intervals. It may answer with qualified confidence or request a targeted retry.

## 11. Retry and degradation policy

The desired production order is:

1. retry transient upload/poll/generation failures with exponential backoff
   and jitter;
2. retry the exact chunk without expanding its identity;
3. split a repeatedly failing chunk into smaller children;
4. use the configured fallback video model;
5. fall back to high-frequency keyframes, OCR, and UI hierarchy around known
   action timestamps;
6. return partial evidence with machine-readable failed intervals.

The runtime implements this order with structured failure classes, bounded
bisection, configured fallback models, and a primary-model circuit breaker.

## 12. Provider and sampling strategy

Provider context caching is an optimization, not the system of record. ARTEMIS
must remain correct if a provider cache misses or expires.

Default 1 FPS video sampling is supplemented by exact, dense seeks around
DataEngine action timestamps. Quiet intervals remain sparse while taps,
transitions, toasts, and spinners receive a higher sampling budget.

## 13. Concurrency

- SQLite uses WAL mode and short `BEGIN IMMEDIATE` claim transactions.
- The unique segment key prevents duplicate rows.
- A UUID lease owner identifies one execution attempt.
- Leases expire so crashes do not permanently block work.
- Existing API and transcode semaphores still bound local concurrency.
- Conflict cleaning affects an analyzer's presentation ledger; it never
  deletes persistent observations written by another analyzer.

## 14. Cleanup and retention

Three artifact classes have different policies:

1. Trimmed/compressed chunks: temporary, deleted after the call.
2. Provider-uploaded files: temporary, deleted best-effort after inference.
3. Blackboard evidence: durable for the trace/session retention period.

Deleting a trace/session removes blackboard rows through DataEngine and
evidence through the session directory lifecycle.

## 15. Observability

Required counters and trace fields:

- requested duration;
- cached duration;
- newly analyzed duration;
- number of claimed, cached, in-progress, succeeded, and failed chunks;
- provider attempts and error class;
- blackboard key and query key;
- partial versus complete outcome;
- evidence paths attached to observations.

No prompt or trace status should report success merely because an error string
was returned as normal text.

## 16. Compatibility and migration

- Public `video_analyzer` and `video_analyzer_pure` arguments do not change.
- Existing in-memory ledger dictionaries remain the presentation format.
- When DataEngine persistence is unavailable, the blackboard falls back to a
  context-scoped in-memory store. This preserves tests and embedded usages but
  does not promise process-restart recovery.
- Databases are migrated with `CREATE TABLE IF NOT EXISTS`; no destructive
  migration is required.
- Existing traces without blackboard rows continue to work and start empty.

## 17. Security and privacy

- Evidence remains local under the trace directory.
- Provider files are deleted best-effort after requests.
- Blackboard rows must not store credentials or entire model prompts.
- Retention follows trace retention.
- Future cross-session or identity memory requires a separate consent and
  privacy design; it is outside this scope.

## 18. Testing strategy

### 18.1 Unit tests

- persistence across two blackboard instances using the same SQLite file;
- deterministic observation deduplication;
- interval union and missing-range calculation;
- exact successful segment reuse;
- live lease suppression and expired lease reclamation;
- failed chunk remains retryable;
- evidence is copied to durable storage;
- two `VideoAnalyzer` instances share prior ledger state;
- repeated `exec_spawn_sub_agent` does not invoke child inference again;
- mixed success/failure returns partial output and preserves success.

### 18.2 Integration tests

- analyze overlapping intervals against a fixture recording;
- interrupt after one successful chunk and resume;
- concurrent identical requests result in one provider call;
- active recording resolves open-ended requests to numeric endpoints;
- evidence remains readable after analyzer cleanup.

### 18.3 Device tests

Per ARTEMIS mobile testing rules, end-to-end mobile flows must be explored and
verified on a connected device before production test scripts are finalized.
The refactor's deterministic unit tests do not require a device; live timing,
fast UI transitions, and recording alignment do.

## 19. Rollout

1. **Complete:** SQLite blackboard, sharing, observation persistence, exact
   coverage reuse, leases, and tests.
2. **Complete:** failure classification, jitter, bounded bisection, fallback,
   and circuit breaking.
3. **Complete:** recording generations, sealed-prefix metadata, and
   generation-scoped caches.
4. **Complete:** action-aware sampling and real audio delivery.
5. **Deferred optimization:** provider file/context caching, gated by an
   explicit retention policy.
6. **Future accuracy layer:** semantic/event hierarchical retrieval after
   production traces provide a representative evaluation corpus.

## 20. Acceptance criteria

- Two analyzer instances in one task see the same ledger.
- Repeating an identical successful interval/query causes zero new child VLM
  calls.
- If one of several chunks fails, successful chunks are available to the next
  invocation and are not rerun.
- A crashed `running` chunk becomes reclaimable after lease expiry.
- Persistent evidence survives normal analyzer temporary-file cleanup.
- Existing video-analyzer unit tests and new blackboard tests pass.

Additional reliability criteria:

- timeout/media failures recover through bounded recursive subdivision;
- provider-wide failures do not trigger request fan-out;
- primary-model failures select the configured `utils.video_analyzer` fallback;
- the primary circuit opens after a configured consecutive-failure threshold;
- audio analysis is independently resumable and reusable;
- recording generation changes invalidate stale trim-cache identities;
- frames are sampled densely around recorded mobile actions;
- traces include blackboard status, attempts, failure categories, successful
  duration, observation count, and circuit state.

## 21. Implemented production architecture

The completed refactor extends the phase-one blackboard with the following
production controls:

1. Failures are normalized as rate limit, provider outage, timeout,
   connection, authentication, bad request, media processing, cancellation,
   or unknown. The class determines retryability, bisection, and fallback.
2. Timeout, media, oversized, and unknown failures can recursively bisect a
   chunk down to configured size/depth bounds. Provider outages, connections,
   and rate limits do not bisect because fan-out would amplify load.
3. Coordinator, video child, and audio child use the configured utility-model
   fallback. A context-scoped circuit breaker bypasses an unhealthy primary.
4. Recording results expose `video_id`, `generation`, `sealed_until`, and
   `source_revision`. Trim caches are scoped by video UUID and generation;
   open-ended requests are resolved to numeric safe ends.
5. Universal video analysis receives sparse baseline frames, dense frames
   around DataEngine mobile-action timestamps, and the real audio track.
   Audio-only analysis sends a standard base64 audio content block. Models
   that reject audio degrade only that video chunk to visual-only analysis.
6. Audio uses the same lease, exact reuse, completion, and failure state
   machine as video.
7. DataEngine session deletion removes blackboard rows by session/video key;
   evidence is deleted with the trace directory. Universal-path temporary
   artifacts are cleaned on early exit.

The persistence schema adds `error_category`, `model_name`, and
`source_generation` to every segment attempt. Migrations are additive and
safe for existing SQLite traces.

## 22. State-of-the-art review and adoption decision

The relevant frontier is not “send the full video to a bigger model.” Recent
systems converge on four architectural ideas:

1. **Agentic coarse-to-fine localization.** [VideoAgent](https://arxiv.org/abs/2403.10517)
   iteratively retrieves a small number of frames; [VideoTree](https://arxiv.org/abs/2405.19209)
   builds a query-adaptive temporal hierarchy.
2. **Persistent hierarchical multimodal memory.** The memory-augmented
   [VideoAgent](https://arxiv.org/abs/2403.11481) separates memory construction
   and query-time tool use. [VideoARM](https://arxiv.org/abs/2512.12360) uses an
   observe-think-act-memorize loop over hierarchical memory.
3. **Adaptive redundancy reduction.** [LongVU](https://arxiv.org/abs/2410.17434)
   removes redundant frames and preserves query-relevant temporal/spatial
   detail instead of sampling uniformly.
4. **Retrieval over very long media.** [VideoRAG](https://arxiv.org/abs/2502.01549)
   combines graph/text grounding with multimodal retrieval. This is useful for
   many-hour corpora, but heavier than one mobile-test recording.

Provider guidance supports the same separation: Gemini's
[video-understanding documentation](https://ai.google.dev/gemini-api/docs/video-understanding)
describes direct audio/video processing and reusable Files API inputs, while
[context caching](https://ai.google.dev/gemini-api/docs/caching) is an
optimization for repeated common input. Neither is a durable work ledger.

### 22.1 Adopted directly

- VideoARM/VideoAgent's observe → reason → act → memorize loop;
- query-scoped coarse-to-fine temporal analysis;
- separation of objective observations and answered query spans;
- adaptive sampling, with mobile action timestamps as the domain-specific
  relevance signal;
- independent visual/audio tools and evidence-grounded synthesis.

### 22.2 Not copied wholesale

The research implementations assume offline benchmark videos, often require
GPU feature extractors, and optimize multiple-choice VideoQA. ARTEMIS operates
on a growing recorder stream, must survive provider/process failures, prevent
duplicate paid calls, and tie evidence to test-relative action time. Importing
VideoARM or VideoRAG would add a second orchestration runtime without solving
leases, idempotent commit, active-recording identity, retention, or mobile
alignment.

ARTEMIS therefore ports their architectural invariants, not their
benchmark-specific code, and supplies the production controls the prototypes
do not provide.

## 23. Runtime configuration

All controls live under `video_analyzer` and flow through the SDK builder:

| Setting | Default | Purpose |
| --- | ---: | --- |
| `chunk_size_seconds` | 60 | Initial coarse chunk size |
| `min_chunk_seconds` | 4 | Smallest bisection leaf |
| `max_split_depth` | 4 | Bound failure-driven fan-out |
| `circuit_breaker_threshold` | 3 | Failures before primary circuit opens |
| `circuit_breaker_cooldown_seconds` | 60 | Half-open delay |
| `action_window_seconds` | 2 | Dense window on either side of an action |
| `dense_action_fps` | 4 | Sampling rate in action windows |
| `max_dense_action_frames` | 24 | Dense-frame budget per chunk |
| `native_max_retries` | 1 | Native-provider retries before universal fallback |
| `model_call_timeout_seconds` | 120 | Hard bound for one model response |

The configured `llm_config.utils.video_analyzer.fallback` supplies the model
fallback. Public video tool signatures remain unchanged.

## 24. Verification status

Local verification covers persistence, exact reuse, leases, partial commit,
structured failures, circuit breaking, fallback selection, timeout bisection,
audio reuse, real audio content blocks, action-aware sampling, exact frame
seeking on an MP4 fixture, generation-scoped caches, configuration
propagation, and existing analyzer/controller behavior.

### 24.1 Real-device acceptance (2026-08-25)

A connected Pixel 10 (`59260DLCR00360`) completed a deterministic Google
Calculator recording with independently observed UI ground truth:

- `12+34=46` at 21.15s;
- `7×8=56` at 32.44s;
- history opened at 39.01s and closed at 42.12s;
- the final screen returned to result `56`.

Three query-independent video analyses all matched that ground truth. Each
query was then replayed twice with the identical interval and query. All six
replays returned `CACHED VIDEO ANALYSIS` in roughly 3ms, with no new child
analysis. Final blackboard state was three succeeded segments, 127.8 covered
query-seconds, and eight timestamped observations. The machine-readable
report and retained MP4 are produced by
`scripts/real_device_video_acceptance.py`.

The run also exercised real provider degradation: native Gemini returned a
503 and later stalled a stream. The affected query preserved prior successes,
retried only its own uncovered work, then completed through the configured
universal fallback. Based on that trace, native attempts now default to two
total calls with a 120-second response bound before fallback.

Device exploration also exposed two non-video runtime faults that affect
perceived stability and were fixed as part of the gate:

1. UIAutomator may return current string/dictionary bounds with a stale cached
   center. Dynamic taps now always derive their center from fresh bounds and
   use the cached center only when bounds are unavailable.
2. A stale desktop IPC port previously blocked every DataEngine event for one
   second. IPC connects now use a short localhost timeout plus exponential
   backoff, and detached agent initialization has a 30-second hard bound.

The acceptance result demonstrates 100% accuracy and reuse for this defined
fixture and run. It is not a mathematical guarantee for every future app,
video, device, or provider outage; production SLOs require a larger repeated
corpus and ongoing monitoring.
