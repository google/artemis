# ARTEMIS Pro Context, History, and Summarization Redesign

Status: Proposal (2026-08-25)

Scope: ARTEMIS Pro / LangGraph execution mode

Audience: maintainers of Operator, Validator, Summarizer, DataEngine, and model routing

## 1. Executive Summary

This proposal improves long-term history, visually grounded neutral summaries,
context compression, and history recall without weakening Pro mode's current
reasoning continuity. The goal is not to give Operator more raw history by
default. It is to increase information density while preserving the current
`10K-12K` input range and moving all additional image interpretation to a
non-blocking background path.

The design preserves mechanisms that already work:

- the previous Operator step's complete raw or native thinking;
- the exact previous action and Validator result;
- the current screenshot and UI element list;
- complete, auditable raw step records in DataEngine; and
- asynchronous Summarizer scheduling that never blocks the graph's critical path.

It adds the following capabilities:

1. Preserve the previous model message, thought signature, action call, and Validator result as a continuous conversation tail.
2. Keep the current neutral summary contract while allowing the Summarizer to use the decision screenshot and an optional post-action screenshot as context.
3. Bind thinking, actions, available images, validation results, and summaries with a stable `step_id`.
4. Treat the active milestone only as a temporary working-set label; use plan-independent history chunks for long-term compression.
5. Add a bounded, on-demand `recall_history` tool for complete steps, older screenshots, and UI text.
6. Introduce a fixed token budget, value-density history selection, and same-turn tool-output limits.
7. Separate stable system rules from dynamic plans and history to improve prefix caching and multi-turn continuity.

The steady-state Operator context becomes:

```text
Current screenshot/XML
  + complete previous-turn reasoning and action result
  + recent visually grounded step summaries
  + relevant history-chunk summaries
  + cold-history recall on demand
```

The steady-state target remains `10K-12K tokens`, with zero additional model
calls and zero summary waits on the critical path.

## 2. Goals and Constraints

### 2.1 First Priority: Preserve Speed

The engineering definition of preserving speed is:

- no new synchronous model calls in normal steps;
- Operator never waits for summaries, indexes, or history chunks;
- first-call input P50 and P95 remain close to the current baseline;
- background summaries do not compete at the same request priority as Operator;
- normal Operator requests still contain only one current screenshot;
- history improvements replace low-density content instead of expanding normal context; and
- any background failure immediately falls back to the current detailed history.

Absolute zero latency change cannot be guaranteed by adding synchronous input.
This design therefore uses a token-neutral, zero-wait critical path and validates
P50 and P90 through paired A/B tests.

### 2.2 History and Summary Goals

- Preserve the previous strategy, expected result, and unresolved questions.
- Ground older step summaries in available visual evidence instead of guessing from action text.
- Keep important history visible across plan rewrites, milestone renames, and goal rollback.
- Make every compressed record traceable to original steps, screenshots, and tool traces.
- Never discard failures, recovery work, side effects, user constraints, or critical values through ordinary compression.
- Allow Operator to recall older information during long tasks.

### 2.3 Use Model Capacity Effectively

"Maximize" means maximizing useful reasoning information, not filling a one-million-token window:

- the strongest model directly handles current visual state, continuous reasoning, and high-value history;
- available older screenshots help the Summarizer understand the UI, action target, and explicit records;
- long context acts as an exception and deep-recall buffer, not the normal working set;
- native multi-turn messages and thought signatures are preserved;
- stable system prefixes are reused whenever possible; and
- the model does not repeatedly reread duplicate plans, raw logs, or low-value click traces.

### 2.4 Non-Goals

- Do not delete or overwrite raw history in DataEngine.
- Do not send multiple historical screenshots to Operator by default.
- Do not treat plan milestones as permanent fact boundaries.
- Do not replace Validator's objective judgment with a summary.
- Do not require a new model API in the first phase.
- Do not enlarge every step merely to use a larger context window.

## 3. Current Baseline

### 3.1 Pro Graph Path

```text
Planner
  -> Convergence
  -> Perception
  -> Operator
  -> Execution Check
  -> Validator
  -> Summarizer
  -> Convergence
```

The Summarizer node creates a background task and returns immediately. This
behavior must remain unchanged.

### 3.2 Current Operator History

`build_plan_and_history` currently:

- renders the latest completed step in detail;
- may include raw/native thinking, actions, tool calls, Validator results, and Failure Analyzer traces;
- renders older summaries when available while retaining exact actions;
- preserves steps under the active subgoal plus the latest three global steps;
- puts the full current plan in the Operator prompt; and
- rebuilds messages on every Operator turn.

The current system already has continuity. Removing complete reasoning from the
latest step would prevent the model from understanding the previous strategy,
expectation, and decision basis.

### 3.3 Remove Separate Short-Term Memory

Remove the existing `<short_term_memory>` extraction, state storage, and next-turn
injection path without introducing a replacement relay object. Cross-turn
continuity should come from the complete previous Operator message, exact action,
Validator result, current plan, long-term Notes, and auditable execution history.

### 3.4 Current Pro Summaries

The current Pro Summarizer receives only text:

- current plan and history;
- current-step thinking;
- action and execution result; and
- Failure Analyzer trace.

It does not load `pre_image_name` or `post_image_name`. Images should reduce
context misunderstanding, not expand the Summarizer's authority to judge results.

### 3.5 Existing Cold History

Complete steps, screenshots, and UI data already exist in DataEngine.
HistoryAnalyzer and Outputter offer partial retrieval, but Operator lacks one
unified direct history-recall tool.

### 3.6 Measured Baseline

Current identifiable Pro Operator calls in `traces/data_engine.db` show:

- 74 direct Operator model calls across 43 Operator turns;
- median input of about 10,221 tokens;
- input P90 of about 11,648 tokens and P95 of about 12,019 tokens;
- maximum input of about 12,747 tokens;
- median model-call latency of about 5.24 seconds and P90 of about 16.42 seconds;
- median full Operator-turn latency of about 20.02 seconds; and
- median observed step interval of about 17.15 seconds.

Input length and model latency show no reliable correlation in the observed
`8.8K-12.7K` range. That result must not be extrapolated to an `80K-100K`
dynamic context.

## 4. Design Principles

### 4.1 Separate Raw Records from the Model View

DataEngine stores complete facts. The prompt contains only the most valuable view
for the current turn. Compression changes what the model sees, never the source record.

### 4.2 Continuous Reasoning Before Compression

The complete previous reasoning, action expectation, action call, and Validator
result are required on the hot path and cannot be replaced by an ordinary summary.

### 4.3 Images Provide Context, Not Judgment

The Summarizer may inspect screenshots that actually exist for a step, but it
must preserve the current prompt's neutral responsibility. It may compress
intent, strategy, explicit verification, iteration progress, missing conditions,
and objective Failure Analyzer intervention. It must not independently decide
whether an action succeeded, failed, completed, or navigated to a new page.

A missing post-action screenshot means only that no independent post-action
visual evidence is available. It never means the screen was unchanged or the
action had no effect.

### 4.4 Plans Are Dynamic Indexes, Not History Keys

The current subgoal may increase relevance but must not control permanent
retention. History identity uses stable `step_id` values, time ranges, and source references.

### 4.5 Validator Facts Take Priority

```text
Explicit user instructions
  > objective Validator or Controller results
  > activity, package, XML, or OCR differences
  > sourced content in visually grounded summaries
  > Operator or Summarizer inference
```

### 4.6 Background Enhancements Cannot Become Dependencies

If a visual summary or history chunk is not ready, Operator immediately continues
with the existing detailed record.

## 5. Target Architecture

```text
                         +-----------------------------+
                         | DataEngine raw facts        |
                         | steps / traces / images / UI|
                         +--------------+--------------+
                                        |
Validator completes step ---------------+
                                        |
                         +--------------v--------------+
                         | Step Memory Record          |
                         | exact action/result/sources |
                         +-------+--------------+-------+
                                 |              |
                         immediate              | asynchronous
                                 |              v
                                 |     +--------------------+
                                 |     | Context Summarizer |
                                 |     | decision image +   |
                                 |     | optional post/UI   |
                                 |     +----------+---------+
                                 |                |
                         +-------v----------------v-------+
                         | Pro Memory Store              |
                         | hot history/summaries/chunks  |
                         +---------------+---------------+
                                         |
Perception current screenshot/XML -------+
                                         v
                         +-----------------------------+
                         | Token-Budget Prompt Builder |
                         +--------------+--------------+
                                        v
                                    Operator
                                        |
                              recall_history on demand
```

## 6. Layered History Model

### 6.1 L0: Current Observation

Every Operator turn receives the current screenshot, package/activity, compact
XML/OCR element list, screen dimensions and coordinate mapping, user-injected
instructions, active plan path, and background-task state. Historical images do
not enter normal requests.

### 6.2 L1: Hot History and Conversation Tail

```text
Previous AIMessage
  - model-visible thinking / thought signature
  - raw reasoning text
  - turn-ending action tool call

Validator Function/Tool Result
  - whether execution occurred
  - execution state
  - Failure Analyzer repair
  - observed post-action state

Current HumanMessage
  - current screenshot
  - current XML
```

Keep one complete prior Operator turn normally and up to two when the last step
failed, remains uncertain, is recovering, or has an unfinished strategy. Do not
duplicate the same complete reasoning in `Execution History`. Until the provider
can preserve real messages and thought signatures, continue rendering raw/native
thinking verbatim.

### 6.3 L2: Visually Grounded, Neutral Step Summaries

Older steps use the current "summary plus exact action" structure, with a
multimodal summary input:

```text
Step N
  Neutral contextual summary
  Exact action
  Validator outcome
  Unresolved/recovery marker
```

The summary never replaces the exact action or overrides Validator. Screenshots
only clarify context; the output does not make a new result judgment.

### 6.4 Current Work Segment

The active milestone may organize recent summaries, loop and candidate progress,
unresolved items, and the most recent entry state. It remains a temporary label
that can change with the plan and never becomes a permanent history identity.

### 6.5 L3: Stable History Chunks

Long-term history is chunked by stable step ranges and token thresholds:

```text
Chunk 1: Steps 1-10
Chunk 2: Steps 11-19
Chunk 3: Steps 20-31
```

Close a chunk after roughly `1.5K-2K tokens` of step summaries or 8-12 steps.
Close early on a major package/activity change, user goal change, Failure Analyzer
boundary, complete strategy change, long work-segment pause, irreversible side
effect, or important external result.

Each chunk preserves its source range and step IDs, attempted path, verified
facts, failed paths, unresolved questions, important entities, entry and exit
states, and raw source references. Later plan rewrites do not alter chunk identity.

### 6.6 L4: Cold History

Retain every screenshot actually saved for a step, raw XML/OCR, raw/native
thinking, complete tool calls and results, ADB and log output, the full Failure
Analyzer trace, all summary versions, Notes, and plan revisions. Cold history is
excluded from normal prompts and loaded only through local matching or
`recall_history`.

## 7. Stable Data Model

### 7.1 StepMemoryRecord

```python
class StepMemoryRecord:
    step_id: UUID
    session_id: UUID
    step_number: int
    timestamp: datetime

    plan_revision: str | None
    current_subgoal_hash: str | None

    operator_message_ref: str | None
    thought_signature: str | None
    raw_thinking_ref: str | None
    native_thinking_ref: str | None
    action: dict
    validator_result: dict | None
    failure_recovery: dict | None

    pre_image_name: str | None
    post_image_name: str | None
    pre_ui_ref: str | None
    post_ui_ref: str | None
    ui_diff: dict | None

    summary: str | None
    summary_status: str
    summary_model: str | None
    summary_version: int

    summary_decision_image_name: str | None
    summary_post_image_name: str | None
    summary_used_ui_diff: bool
```

The `summary_status` state machine is:

```text
pending -> visual_ready -> visual_failed -> stale
```

Exact actions, Validator results, and raw sources remain immediately available
regardless of summary state.

### 7.2 SummaryEvidenceMetadata

The summary remains one neutral natural-language paragraph. Deterministic source
metadata is stored separately:

```json
{
  "step_id": "...",
  "decision_image_name": "image-a",
  "post_image_name": null,
  "used_ui_diff": false,
  "model": "gemini-3.7-flash",
  "prompt_version": 2,
  "summary_version": 3
}
```

The system derives these fields from actual inputs. A null `post_image_name`
expresses no action result or screen-change judgment.

### 7.3 HistoryChunk

```python
class HistoryChunk:
    chunk_id: UUID
    session_id: UUID
    start_step_id: UUID
    end_step_id: UUID
    source_step_ids: list[UUID]
    digest: str
    verified_facts: list[str]
    unresolved: list[str]
    failed_paths: list[str]
    important_entities: list[str]
    entry_state: str | None
    exit_state: str | None
    model: str
    version: int
```

Chunk summaries are versioned append-only records and never damage source summaries.

## 8. Visually Grounded Neutral Pro Summarizer

### 8.1 Inputs

Each background summary call uses only inputs that actually exist:

1. the decision screenshot seen by Operator, optionally annotated with action coordinates;
2. an independent post-action screenshot only when Validator captured and saved it;
3. the exact action name and parameters;
4. the Controller result;
5. the final Validator result;
6. Failure Analyzer recovery records;
7. available package/activity information;
8. available UI/OCR differences;
9. current-step thinking; and
10. a small number of recent summaries for loop and strategy continuity.

Do not resend the full plan and ten detailed steps. Build the message dynamically,
never substitute the decision screenshot for a missing post-action screenshot,
and never infer "no screen change" from missing post-action evidence.

### 8.2 Output Rules

`artemis/agents/summarizer/summarizer.md` remains the sole authority for summary
semantics. Preserve its philosophy and one-paragraph output. Use Operator's first
person to compress intent, strategy, explicit progress, and verified facts. Do
not independently claim success, failure, completion, non-completion, or page
navigation. A successful Controller return does not prove task success. Do not
guess unreadable values or judge outcomes by whether two images look similar.

Failure Analyzer intervention is summarized as an objective trigger and action,
without claiming that the repair worked. Step ID, evidence names, model, prompt
version, and summary version are system metadata rather than model-generated text.

The existing prohibition on outcome-judgment terms remains, including
`successfully`, `completed`, `entered`, `navigated to`, `failed`, `unsuccessful`,
`could not`, and `achieved`.

### 8.3 Model Selection

Quality-first default:

```text
Per-step summary: Gemini 3.7 Flash + available screenshots
History-chunk summary: Gemini 3.7 Flash
```

Single-quota, strict-isolation option:

```text
Real-time step summary: Gemini 3.5 Flash-Lite + available screenshots
Idle reprocessing for violations or important omissions: Gemini 3.7 + available screenshots
History-chunk summary: Gemini 3.7 Flash
```

The recommended quality ceiling remains 3.7 with available images. Flash-Lite is
a resource-isolation option, not a replacement for complex strategy, loop, and
recovery reasoning.

### 8.4 Scheduling and Isolation

- Operator never awaits Summarizer.
- Background summary concurrency is limited to one.
- Operator requests have absolute priority.
- Summary timeout or failure never affects graph execution.
- Existing detailed history remains the fallback.
- Prefer a separate endpoint, project, or quota.
- Without resource isolation, do not start a new 3.7 summary while Operator is active.
- Give non-preemptible background calls a short timeout.
- An optional final flush must not delay the next normal step.

## 9. Stable History Chunks

Milestones reflect a temporary interpretation of the task and are not stable fact
boundaries. A plan edit can otherwise hide relevant steps, split one path across
milestones, make rollback lose a prior strategy, or desynchronize summaries from
current labels. Subgoal identity therefore affects relevance only.

```python
if current_open_chunk.summary_tokens >= 1800:
    close_chunk()
elif current_open_chunk.step_count >= 12:
    close_chunk()
elif important_boundary_event:
    close_chunk()
```

Prevent recursive summary loss by generating each chunk from source step
summaries, retaining `source_step_ids`, merging structured fact sets rather than
guessing from prose, and retaining every summary version.

## 10. Operator Cold-History Recall

Expose one unified tool:

```python
recall_history(
    query: str,
    step_range: list[int] | None = None,
    include_details: bool = False,
    include_images: bool = False,
    max_results: int = 5,
) -> HistoryRecallResult
```

Search visually grounded summaries, history chunks, raw actions, Validator
results, Operator thinking, Notes, XML/OCR text, package/activity data, Failure
Analyzer traces, and image references. Phase one can use SQLite FTS, keywords,
step ranges, package/activity, and screen hashes without another model or vector database.

Return at most five results and 2K tokens by default. Every result includes a
step ID or number. Detailed output remains token-bounded. Image recall returns no
more than the images that actually exist for one step. Large raw results return
only relevant excerpts and references.

Operator should recall history when the current screen resembles an older state,
an old exact value is needed, two consecutive steps make no progress, the same
action is about to be attempted a third time, a summary conflicts with current
observation, the user asks to return to an earlier state, or Failure Analyzer
needs review. Ordinary steps must not recall history speculatively.

Before prompt construction, a local package/activity, screenshot-hash, and UI-
signature match may inject a short hint without a model call or historical image:

```text
Historical state hint: The current screen closely resembles the post-action
screen from Step 17. Use recall_history only if its details are needed.
```

## 11. Token Budget and History Selection

Recommended steady-state budgets:

```text
First Operator call target:      10K-12K
First-call soft limit:           12K
First-call emergency hard limit: 16K
Same-turn soft limit:            20K
Same-turn hard limit:            28K
```

```python
history_budget = (
    operator_total_budget
    - stable_system_tokens
    - tool_schema_tokens
    - current_observation_tokens
    - active_plan_tokens
    - same_turn_reserve
)
```

Always retain the previous complete Operator turn, current user constraints,
active plan path, unresolved items, recent failures and recovery, irreversible
actions or external side effects, the previous chunk's exit state, and current
loop or candidate progress.

Score optional history using current-subgoal relevance, unresolved status,
failure or recovery relevance, screen similarity, entity overlap, path boundary,
explicit user references, recency, resolved status, and duplication. Fill the
remaining budget by `value_density = priority / token_cost`.

When over budget:

1. Remove duplicate renderings.
2. Keep the previous real message and remove its duplicate detailed history text.
3. Replace older detailed steps with visual summaries.
4. Replace groups of old summaries with stable chunks.
5. Remove resolved, unrelated, low-value chunks.
6. Keep only relevant excerpts and references from large tool output.

Never evict hard user constraints, unresolved items, irreversible side effects,
critical values, recent recovery state, or the previous complete reasoning and expectation.

## 12. Prompt and Cache Layout

Keep the stable Operator role, safety rules, cognitive protocol, and output
contract in `SystemMessage`. Put the previous real AI message, thought signature,
action call, and Validator tool result in the conversation tail. Put the current
goal, active plan path, budgeted history, current screenshot, and UI elements in
the dynamic `HumanMessage`. Keep tool schemas and ordering stable when possible.

If the provider adapter supports stateful interactions, preserve one to three
recent Operator interactions with `previous_interaction_id`. Start a new
interaction when the token threshold is reached or the plan changes materially,
seeding it with the stable prefix and chunk summaries. Until then, retain the
current message-rebuild path.

## 13. Same-Turn Context Control

Large ADB, Notes, Explorer, log, or XML results can expand later model calls in
the same Operator turn. Store complete results in a trace or artifact and expose
a local envelope:

```json
{
  "status": "success",
  "important_lines": ["..."],
  "errors": [],
  "tail": ["..."],
  "full_result_ref": "trace://...",
  "truncated": true,
  "original_chars": 87321
}
```

Apply character, line, and estimated-token limits; retain matched lines, errors,
and head/tail context; never call another LLM synchronously to summarize tool
output. Compress older results at the soft limit and require a decision instead
of accumulating more exploration at the hard limit.

## 14. Configuration

Add a dedicated Pro memory section instead of reusing Flash `step_summarizer`:

```jsonc
"agent": {
  "pro": {
    "memory": {
      "enabled": true,
      "preserve_previous_operator_turn": true,
      "max_hot_turns": 2,
      "operator_target_input_tokens": 12000,
      "operator_hard_input_tokens": 16000,
      "same_turn_soft_tokens": 20000,
      "same_turn_hard_tokens": 28000,
      "visual_step_summary": {
        "enabled": true,
        "model": "gemini-3.7-flash",
        "thinking_level": "low",
        "include_pre_image": true,
        "include_post_image_when_available": true,
        "include_ui_diff": true,
        "max_concurrency": 1,
        "operator_never_waits": true
      },
      "history_chunks": {
        "enabled": true,
        "max_steps": 12,
        "target_source_tokens": 1800,
        "model": "gemini-3.7-flash"
      },
      "recall": {
        "enabled": true,
        "max_results": 5,
        "max_text_tokens": 2000,
        "max_image_steps": 1
      }
    }
  }
}
```

For a speed-first single-quota deployment, override only the visual step model
with `gemini-3.5-flash-lite`.

## 15. Implementation Scope

### 15.1 Operator

Update Operator, prompt builders, and configuration to add a token-budget memory
assembler, preserve recent real messages and Validator results, avoid duplicate
history rendering, move dynamic state out of the stable system prompt, remove
`<short_term_memory>`, add `recall_history`, inject local screen-similarity hints,
and enforce same-turn output limits.

### 15.2 Summarizer

Load decision and optional post-action images by stable step ID, include available
Validator and UI-diff data, send multimodal messages, preserve the neutral
one-paragraph contract, atomically update versioned evidence metadata, retain old
summaries on failure, enforce concurrency and timeout limits, and never block Operator.

### 15.3 Graph and State

Store the previous Operator `AIMessage`, thought signature, and Validator function
response. Remove the `short_term_memory` field while safely ignoring it in older
tasks. Read ready memory snapshots at the Perception/Operator boundary and retain
the current asynchronous Summarizer semantics.

### 15.4 DataEngine

Add summary status, version, evidence, thought-signature, message-reference, and
UI-diff fields; persist history chunks and local recall indexes; support atomic
updates by step ID; and preserve every older summary version and source.

### 15.5 Task Tree

Separate history visibility from rendering detail, render visual summaries and
stable chunks, use current subgoal only for relevance, avoid repeating the latest
step when it is already present in the real message tail, and retain exact actions
and validation results.

### 15.6 Memory Module

Suggested files:

```text
artemis/memory/pro_memory.py
artemis/memory/history_chunker.py
artemis/memory/history_retriever.py
artemis/memory/token_budget.py
artemis/tools/history_recall.py
```

Responsibilities include memory snapshots, value-based selection, chunk closure
and background summarization, FTS and screen-signature retrieval, the recall tool,
and provider-independent token estimation.

### 15.7 Configuration

Every capability must have an independent switch, default to current Pro fallback
behavior, tolerate missing configuration and older tasks, and support both the
3.7 quality mode and Flash-Lite isolation mode.

## 16. Delivery Phases

### Phase 0: Baseline and Observability

Freeze a representative long-task suite. Record input tokens, TTFT, model latency,
full-turn latency, summary-ready latency, failures, retries, history size,
duplicate-action rate, and Failure Analyzer activation without changing prompts.

### Phase 1: Protect and Improve Hot History

Preserve complete recent reasoning, remove the duplicate short-term-memory path,
avoid duplicate rendering, add the total token threshold, and locally truncate
large tool envelopes. Prove that speed does not regress first.

### Phase 2: Pro Visual Step Summaries

Load actual decision and optional post-action images by stable step ID while
preserving the neutral output contract and text fallback. Compare 3.7 with images,
Flash-Lite with images, and current 3.7 text-only behavior. Confirm no Operator wait.

### Phase 3: Stable History Chunks

Add milestone-independent rolling chunks, generate them from source summaries,
treat milestones as temporary working-set labels, and add source references and versions.

### Phase 4: Operator Cold-History Recall

Reuse step, screenshot, and UI search capabilities behind one bounded
`recall_history` tool, add local screen-similarity hints, and limit text and images.

### Phase 5: Real Multi-Turn Messages and Cache Optimization

Preserve real AI messages, tool calls, and thought signatures; separate stable
system and dynamic human content; evaluate stateful provider APIs; and safely
restart interactions at token thresholds. Begin only after the first four phases stabilize.

## 17. Test Plan

Unit tests must cover hot-history preservation, removal of short-term-memory
parsing, action/Validator pairing, duplicate suppression, two-turn recovery,
image lookup by step ID, optional post-image behavior, neutral-output validation,
Validator precedence, concurrency, timeout, zero-wait behavior, chunk thresholds,
source traceability, recall bounds, image limits, and hard token protections.

Integration tests must include tasks longer than 30 steps, repeated similar
screens, plan rewrites, cross-app flows, dialogs, Toasts, WebViews, Canvas,
Failure Analyzer recovery, repeated ADB exploration, long lists, monitoring loops,
summary timeout or quota exhaustion, and exact-value recall from older screenshots.

Run blinded evaluations on real traces comparing 3.7 text-only, Flash-Lite with
available images, and 3.7 with available images. Reviewers score fidelity to
intent and explicit facts, unsupported visual claims, unauthorized outcome
judgments, anomaly retention, next-step usefulness, generation time, and ready latency.

## 18. Acceptance Criteria

Under the same device, task, model, and comparable service load:

- first-call input P50 increases by no more than 3% and P95 by no more than 5%;
- Operator model-call and full-turn P50/P90 latency regress by no more than 5%;
- observed step-interval P50/P90 regress by no more than 5%;
- Operator waits for summaries exactly zero times;
- normal Operator requests contain zero historical images; and
- images returned by `recall_history(include_images=True)` count only as explicit recall.

Run at least 30 paired tasks and report confidence intervals. Quality must not
regress for previous-strategy recall, overall task success, or Failure Analyzer
recovery. Visual-fact accuracy, important-fact retention across plan edits,
repeat-action rate, history-induced rollback rate, and top-five recall hit rate
must improve. Summary/Validator conflict should approach zero.

## 19. Failure Modes and Fallbacks

- If a visual summary is pending, use complete previous reasoning, exact action, and Validator result.
- If visual summarization fails, set `visual_failed`, preserve old summaries, and do not block for retries.
- If screenshot evidence is incomplete, include only actual evidence and never infer the missing post-action state.
- If chunk generation fails, keep source summaries and retry the pending chunk during an idle window.
- If recall output is too large, return high-scoring excerpts and step/artifact references.
- If background work competes with Operator, switch to Lite, pause new summaries, or use a separate quota.
- If stateful interactions are incompatible, return to prompt reconstruction while preserving raw/native thinking.

## 20. Rollout Flags

```text
pro_memory_enabled
preserve_operator_message_tail
visual_step_summary
history_chunks
operator_history_recall
stateful_operator_interactions
```

Recommended order: enable metrics and token thresholds; run visual summaries in
shadow mode; use them after blinded validation; enable stable chunks; enable
`recall_history`; then evaluate real multi-turn interactions. Disabling any flag
must restore current Pro behavior.

## 21. Improvements Over the Current System

| Capability | Current Pro | Redesigned Pro |
|---|---|---|
| Previous reasoning | Re-rendered as detailed history | Preserved and ready for a real message/thought-signature chain |
| Short-term strategy | Optional `<short_term_memory>` | Carried by the complete previous message, Validator, plan, and Notes |
| Step summary | 3.7 reads text only | Neutral 3.7 summary reads actual available images, UI, and Validator data |
| Exact action | Retained beside summary | Still retained and never replaced by visual summary |
| History pruning | Current subgoal plus latest three steps | Working set plus stable chunks and relevance budget |
| Plan rewrite | May change old-step visibility | Stable step/chunk references survive label changes |
| Old screenshots | No direct Operator recall | One actual step can be recalled on demand |
| Cold history | Primarily for HistoryAnalyzer/Outputter | Locally searchable and bounded for Operator |
| Context control | Mainly a step window | Total token budget, value density, and same-turn limits |
| Cache use | Highly dynamic system prompt | Stable system prefix separated from runtime state |
| Background failure | Summary may be delayed | Explicit state machine and zero-wait fallback |

The objective is not to display more history. It is to preserve recent reasoning,
give older summaries real visual context, decouple long-term identity from plan
milestones, make compressed records precisely recallable, and keep normal input
size and critical-path model-call count unchanged.

## 22. Recommended Defaults

```text
Normal Operator input:             10K-12K tokens
First-call soft/hard limits:       12K / 16K
Same-turn soft/hard limits:        20K / 28K

Complete hot turns:                1 normally, up to 2 for failure/uncertainty
Current-work summaries:            budget-controlled, not a fixed count
History chunk:                     8-12 steps or about 1.8K source-summary tokens

Historical images in normal input: 0
Summary input images:              0-1 decision, 0-1 independent post-action
Explicit recall images:            actual images from at most one step

Preferred step-summary model:      Gemini 3.7 Flash + images
Single-quota speed mode:           Gemini 3.5 Flash-Lite + images
History-chunk model:               Gemini 3.7 Flash
Background summary concurrency:    1

Additional critical-path calls:    0
Critical-path summary waits:       0
```

## 23. Final Decision

Do not remove previous reasoning and replace detailed history with an image-free
capsule. Preserve complete previous-turn reasoning, upgrade older steps to
visually grounded neutral summaries, use milestone-independent rolling chunks,
allow bounded cold-history recall, and enforce a fixed token budget.

The first, highest-value implementation slice is:

1. preserve complete previous reasoning and remove the duplicate short-term-memory path;
2. add the decision screenshot and optional actual post-action screenshot to the current neutral Pro Summarizer;
3. bind summaries with stable step IDs; and
4. establish speed baselines and blinded visual-summary evaluations.

Do not expand the normal history window or remove the current detailed-history
fallback until all four items have passed validation.
