import { consolidateLogsToBlocks, getSortedStepEvents } from './stream-aggregator.util';

describe('stream aggregator timeline ordering', () => {
  it('interleaves text, tools, and actions by timestamp', () => {
    const events = getSortedStepEvents({
      timestamp: 10,
      operator_native_thinking: 'Thought content',
      operator_native_thinking_timestamp: 11,
      operator_raw_thinking: 'Work content',
      operator_raw_thinking_timestamp: 13,
      action_taken: { action: 'tap', timestamp: 15 },
      generic_tools: [
        { trace_id: 'tool-late', name: 'save_note', timestamp: 14 },
        { trace_id: 'tool-early', name: 'video_analyzer', timestamp: 12 }
      ]
    });

    expect(events.map((event) => event.type)).toEqual([
      'thinking',
      'tool',
      'text',
      'tool',
      'action'
    ]);
    expect(events.map((event) => event.timestamp)).toEqual([
      11000,
      12000,
      13000,
      14000,
      15000
    ]);
  });

  it('uses a deterministic text-first fallback when only a step timestamp exists', () => {
    const events = getSortedStepEvents({
      timestamp: 20,
      operator_native_thinking: 'Thought content',
      operator_raw_thinking: 'Work content',
      action_taken: { action: 'tap' },
      generic_tools: [{ trace_id: 'tool-1', name: 'save_note' }]
    });

    expect(events.map((event) => event.type)).toEqual([
      'thinking',
      'text',
      'action',
      'tool'
    ]);
  });

  it('keeps the first live timestamp when later chunks update the same text stream', () => {
    const blocks = consolidateLogsToBlocks([
      {
        type: 'llm_stream',
        timestamp: '2026-08-25T20:00:01.000Z',
        data: {
          execution_id: 'exec-1',
          step_id: 'step-1',
          stream_type: 'text',
          text: 'First',
          isCompleted: false
        }
      },
      {
        type: 'trace_recorded',
        timestamp: '2026-08-25T20:00:02.000Z',
        data: {
          trace_id: 'tool-1',
          step_id: 'step-1',
          type: 'tool',
          name: 'save_note',
          timestamp: '2026-08-25T20:00:02.000Z'
        }
      },
      {
        type: 'llm_stream',
        timestamp: '2026-08-25T20:00:03.000Z',
        data: {
          execution_id: 'exec-1',
          step_id: 'step-1',
          stream_type: 'text',
          text: 'First and second',
          isCompleted: false
        }
      }
    ]);

    expect(blocks[0].data.operator_raw_thinking_timestamp)
      .toBe('2026-08-25T20:00:01.000Z');
    expect(getSortedStepEvents(blocks[0].data).map((event) => event.type))
      .toEqual(['text', 'tool']);
  });
});

describe('stream aggregator checker lane', () => {
  const started = {
    type: 'checker_event',
    timestamp: '2026-08-25T20:00:05.000Z',
    data: {
      event: 'attempt_started',
      phase: 'checkpoint',
      attempt_id: 'abc#1',
      checkpoint_id: 'abc',
      subgoal_text: 'Create the alarm',
      trace_id: 'trace-checker-1',
      items: [{ kind: 'verify', text: 'alarm exists' }],
      ts: 1756152005
    }
  };

  it('creates a running checker block from attempt_started and finishes it by attempt id', () => {
    const blocks = consolidateLogsToBlocks([
      started,
      {
        type: 'checker_event',
        timestamp: '2026-08-25T20:00:09.000Z',
        data: {
          event: 'attempt_finished',
          attempt_id: 'abc#1',
          status: 'done',
          verdicts: [{ item_text: 'alarm exists', kind: 'verify', status: 'passed', evidence: 'seen' }],
          findings: [],
          ts: 1756152009
        }
      }
    ]);

    expect(blocks.length).toBe(1);
    expect(blocks[0].type).toBe('checker');
    expect(blocks[0].id).toBe('checker-abc#1');
    expect(blocks[0].data.isCompleted).toBeTrue();
    expect(blocks[0].data.status).toBe('done');
    expect(blocks[0].data.trace_id).toBe('trace-checker-1');
    expect(blocks[0].data.verdicts.length).toBe(1);
    expect(blocks[0].data.duration).toBe(4);
  });

  it("routes the checker's stream and tool traces by parent trace id, not by the current step", () => {
    const blocks = consolidateLogsToBlocks([
      {
        type: 'step_recorded',
        timestamp: '2026-08-25T20:00:01.000Z',
        data: { step_id: 'step-1', step_number: 1, timestamp: 1756152001 }
      },
      started,
      {
        type: 'llm_stream',
        timestamp: '2026-08-25T20:00:06.000Z',
        data: {
          execution_id: 'exec-checker',
          step_id: 'step-1',
          parent_trace_id: 'trace-checker-1',
          stream_type: 'thinking',
          text: 'Inspecting the alarm list',
          isCompleted: false
        }
      },
      {
        type: 'trace_recorded',
        timestamp: '2026-08-25T20:00:07.000Z',
        data: {
          trace_id: 'tool-probe',
          parent_trace_id: 'trace-checker-1',
          agent_name: 'checker',
          type: 'tool',
          name: 'probe_device',
          step_id: 'step-1',
          status: 'success'
        }
      }
    ]);

    const step = blocks.find((b) => b.id === 'step-step-1')!;
    const checker = blocks.find((b) => b.id === 'checker-abc#1')!;
    expect(step.data.operator_native_thinking).toBeUndefined();
    expect(step.data.generic_tools || []).toEqual([]);
    expect(checker.data.operator_native_thinking).toBe('Inspecting the alarm list');
    expect(checker.data.generic_tools.map((t: any) => t.trace_id)).toEqual(['tool-probe']);
    expect(checker.data.step_id).toBeUndefined();
    expect(blocks.map((b) => b.id)).toEqual(['step-step-1', 'checker-abc#1']);
  });

  it('keeps one stream segment per checker turn so text and tool calls interleave by time', () => {
    const turn = (execId: string, timestamp: string, text: string) => ({
      type: 'llm_stream',
      timestamp,
      data: {
        execution_id: execId,
        parent_trace_id: 'trace-checker-1',
        stream_type: 'text',
        text,
        isCompleted: false
      }
    });
    const blocks = consolidateLogsToBlocks([
      started,
      turn('exec-1', '2026-08-25T20:00:06.000Z', 'Let me look at step 2.'),
      {
        type: 'trace_recorded',
        timestamp: '2026-08-25T20:00:07.000Z',
        data: {
          trace_id: 'tool-detail',
          parent_trace_id: 'trace-checker-1',
          agent_name: 'checker',
          type: 'tool',
          name: 'get_step_detail',
          status: 'success',
          timestamp: '2026-08-25T20:00:07.000Z'
        }
      },
      turn('exec-2', '2026-08-25T20:00:08.000Z', 'The alarm is there.'),
      turn('exec-1', '2026-08-25T20:00:08.500Z', 'Let me look at step 2. Done.')
    ]);

    const checker = blocks.find((b) => b.id === 'checker-abc#1')!;
    expect(checker.data.stream_segments.map((s: any) => s.execution_id)).toEqual(['exec-1', 'exec-2']);
    // A later chunk of an earlier turn keeps that turn's first-seen time.
    expect(checker.data.stream_segments[0].text).toBe('Let me look at step 2. Done.');
    expect(checker.data.stream_segments[0].timestamp).toBe('2026-08-25T20:00:06.000Z');
    const events = getSortedStepEvents(checker.data);
    expect(events.map((e) => e.type)).toEqual(['text', 'tool', 'text']);
    expect(events[0].data.text).toBe('Let me look at step 2. Done.');
    expect(events[2].data.text).toBe('The alarm is there.');
  });

  it('builds the run outcome block once and keeps it idempotent', () => {
    const outcome = {
      type: 'checker_event',
      timestamp: '2026-08-25T20:00:20.000Z',
      data: { event: 'run_outcome', task_status: 'completed', tests: { passed: 1, failed: 0, inconclusive: 0, unchecked: 0 } }
    };
    const blocks = consolidateLogsToBlocks([outcome, outcome]);
    expect(blocks.length).toBe(1);
    expect(blocks[0].id).toBe('checker-outcome');
    expect(blocks[0].data.phase).toBe('outcome');
    expect(blocks[0].data.task_status).toBe('completed');
  });
});

describe('stream aggregator step ownership', () => {
  const plannerStream = {
    type: 'llm_stream',
    timestamp: '2026-09-02T03:57:25.000Z',
    data: { execution_id: 'exec-planner', stream_type: 'text', text: 'I have formulated the task plan.', isCompleted: true }
  };
  const tap = { trace_id: 'trace-tap-3', type: 'action', name: 'tap', timestamp: 1788374289.37, payload: { args: { action: 'tap' } } };

  it('does not fold a step without streamed text into an earlier untagged stream block', () => {
    const blocks = consolidateLogsToBlocks([
      plannerStream,
      {
        type: 'step_recorded',
        timestamp: '2026-09-02T03:57:47.000Z',
        data: { step_id: 's1', step_number: 1, operator_raw_thinking: 'Launching Maps', generic_tools: [] }
      },
      {
        type: 'step_recorded',
        timestamp: '2026-09-02T03:58:09.000Z',
        data: { step_id: 's3', step_number: 3, generic_tools: [tap] }
      }
    ]);

    expect(blocks.map((b) => b.id)).toEqual(['stream-exec-planner', 'step-s1', 'step-s3']);
    expect(blocks[0].type).toBe('llm_stream');
    expect(blocks[0].data.generic_tools).toEqual([]);
    expect(blocks[0].data.operator_raw_thinking).toBe('I have formulated the task plan.');
    expect(blocks[2].data.generic_tools.map((t: any) => t.trace_id)).toEqual(['trace-tap-3']);
  });

  it('creates the step block for a tagged trace that arrives before its step is recorded', () => {
    const blocks = consolidateLogsToBlocks([
      {
        type: 'step_recorded',
        timestamp: '2026-09-02T03:58:03.000Z',
        data: { step_id: 's2', step_number: 2, operator_raw_thinking: 'Dismissing the dialog', generic_tools: [] }
      },
      {
        type: 'trace_recorded',
        timestamp: '2026-09-02T03:58:09.300Z',
        data: { ...tap, step_id: 's3', status: 'running' }
      },
      {
        type: 'step_recorded',
        timestamp: '2026-09-02T03:58:09.400Z',
        data: { step_id: 's3', step_number: 3, generic_tools: [{ ...tap, status: 'success' }] }
      }
    ]);

    expect(blocks.map((b) => b.id)).toEqual(['step-s2', 'step-s3']);
    expect(blocks[0].data.generic_tools).toEqual([]);
    expect(blocks[1].data.generic_tools.length).toBe(1);
    expect(blocks[1].data.generic_tools[0].status).toBe('success');
  });

  it('still attaches untagged traces to the latest preceding block', () => {
    const blocks = consolidateLogsToBlocks([
      plannerStream,
      {
        type: 'trace_recorded',
        timestamp: '2026-09-02T03:57:26.000Z',
        data: { trace_id: 'trace-note', type: 'tool', name: 'save_note', payload: { args: {} } }
      }
    ]);

    expect(blocks.length).toBe(1);
    expect(blocks[0].data.generic_tools.map((t: any) => t.trace_id)).toEqual(['trace-note']);
  });
});

describe('stream aggregator checker history relocation', () => {
  it('moves persisted checker tool traces from the operator step to the attempt block', () => {
    const blocks = consolidateLogsToBlocks([
      {
        type: 'step_updated',
        timestamp: '2026-08-25T20:00:01.000Z',
        history_snapshot: true,
        data: {
          step_id: 'step-1',
          step_number: 1,
          timestamp: 1756152001,
          generic_tools: [
            { trace_id: 'trace-checker-1', type: 'agent', name: 'checker', status: 'success' },
            { trace_id: 'tool-probe', parent_trace_id: 'trace-checker-1', type: 'tool', name: 'probe_device', status: 'success' },
            { trace_id: 'tool-note', parent_trace_id: 'operator-trace', type: 'tool', name: 'save_note', status: 'success' }
          ]
        }
      },
      {
        type: 'checker_event',
        timestamp: '2026-08-25T20:00:09.000Z',
        checks_snapshot: true,
        data: {
          event: 'attempt_finished',
          phase: 'checkpoint',
          attempt_id: 'abc#1',
          checkpoint_id: 'abc',
          subgoal_text: 'Create the alarm',
          trace_id: 'trace-checker-1',
          status: 'done',
          verdicts: [{ item_text: 'alarm exists', kind: 'verify', status: 'passed', evidence: 'seen' }],
          ts: 1756152009
        }
      }
    ]);

    const step = blocks.find((b) => b.id === 'step-step-1')!;
    const checker = blocks.find((b) => b.id === 'checker-abc#1')!;
    expect(step.data.generic_tools.map((t: any) => t.trace_id)).toEqual(['tool-note']);
    expect(checker.data.generic_tools.map((t: any) => t.trace_id)).toEqual(['tool-probe']);
  });
});

describe('stream aggregator single source of truth (Option A)', () => {
  it('does not duplicate action_taken when generic_tools already contains the action trace', () => {
    const events = getSortedStepEvents({
      timestamp: 100,
      action_taken: [{ action: 'launch_app', app_name: 'Maps', timestamp: 102 }],
      generic_tools: [
        { trace_id: 'tool-note', type: 'tool', name: 'update_note', timestamp: 101 },
        { trace_id: 'act-launch', type: 'action', name: 'launch_app', timestamp: 102, payload: { args: { action: { action: 'launch_app', app_name: 'Maps' } } } }
      ]
    });

    expect(events.length).toBe(2);
    expect(events.map(e => e.type)).toEqual(['tool', 'action']);
    expect(events.map(e => e.data.name)).toEqual(['update_note', 'launch_app']);
  });

  it('retains multiple adb commands and tools alongside physical actions without dropping any', () => {
    const events = getSortedStepEvents({
      timestamp: 100,
      action_taken: [{ action: 'wait_for_delay', time_in_ms: 1000, timestamp: 105 }],
      generic_tools: [
        { trace_id: 'adb-1', type: 'tool', name: 'run_adb_command', timestamp: 101, payload: { CommandLine: 'dumpsys telephony.registry' } },
        { trace_id: 'adb-2', type: 'tool', name: 'run_adb_command', timestamp: 102, payload: { CommandLine: 'am start -a ...' } },
        { trace_id: 'exp-1', type: 'tool', name: 'ask_explorer', timestamp: 103 },
        { trace_id: 'act-wait', type: 'action', name: 'wait_for_delay', timestamp: 104, payload: { args: { time_in_ms: 1000 } } }
      ]
    });

    expect(events.length).toBe(4);
    expect(events.map(e => e.type)).toEqual(['tool', 'tool', 'tool', 'action']);
    expect(events.map(e => e.data.name)).toEqual(['run_adb_command', 'run_adb_command', 'ask_explorer', 'wait_for_delay']);
  });

  it('supports multi-action bursts sequentially from generic_tools', () => {
    const events = getSortedStepEvents({
      timestamp: 100,
      action_taken: [{ action: 'tap', coordinates: [200, 300] }],
      generic_tools: [
        { trace_id: 'tap-1', type: 'action', name: 'tap', timestamp: 101, payload: { args: { action: { action: 'tap', coordinates: [100, 200] } } } },
        { trace_id: 'tap-2', type: 'action', name: 'tap', timestamp: 102, payload: { args: { action: { action: 'tap', coordinates: [200, 300] } } } }
      ]
    });

    expect(events.length).toBe(2);
    expect(events.every(e => e.type === 'action')).toBeTrue();
    expect(events[0].data.trace_id).toBe('tap-1');
    expect(events[1].data.trace_id).toBe('tap-2');
  });

  it('correctly processes real trace 38fd934e Step 16 keeping ADB commands and wait_for_delay visible without duplicates', () => {
    const step16 = {
      step_id: '31e8c7ec-6b67-485e-94f4-355a853ccf8a',
      step_number: 16,
      action_taken: [{ action: 'wait_for_delay', time_in_ms: 1000 }],
      operator_native_thinking: 'Let me check telephony and send SMS...',
      operator_native_thinking_timestamp: 1788457630,
      timestamp: 1788457630,
      generic_tools: [
        { trace_id: 't-adb-1', parent_trace_id: 't-op', name: 'run_adb_command', type: 'tool', timestamp: 1788457631, status: 'success', payload: { args: { CommandLine: 'dumpsys telephony.registry' } } },
        { trace_id: 't-adb-1-sub', parent_trace_id: 't-adb-1', name: 'run_adb_command', type: 'tool', timestamp: 1788457631.5, status: 'success' },
        { trace_id: 't-adb-2', parent_trace_id: 't-op', name: 'run_adb_command', type: 'tool', timestamp: 1788457632, status: 'success', payload: { args: { CommandLine: 'am start -a android.intent.action.SENDTO ...' } } },
        { trace_id: 't-adb-2-sub', parent_trace_id: 't-adb-2', name: 'run_adb_command', type: 'tool', timestamp: 1788457632.5, status: 'success' },
        { trace_id: 't-exp', parent_trace_id: 't-op', name: 'ask_explorer', type: 'tool', timestamp: 1788457633, status: 'success', payload: { args: { query: 'any text on screen' } } },
        { trace_id: 't-exp-sub', parent_trace_id: 't-exp', name: 'ask_explorer', type: 'tool', timestamp: 1788457633.5, status: 'success' },
        { trace_id: 't-wait', parent_trace_id: 't-val', name: 'wait_for_delay', type: 'action', timestamp: 1788457635, status: 'success', payload: { args: { action: { action: 'wait_for_delay', time_in_ms: 1000 } } } },
        { trace_id: 't-safety', parent_trace_id: 't-wait', name: 'safety_net_pixel_validation', type: 'tool', timestamp: 1788457636, status: 'success' }
      ]
    };

    const events = getSortedStepEvents(step16);
    // 1 native thinking + 2 adb tools + 1 explorer tool + 1 wait_for_delay action
    expect(events.length).toBe(5);
    expect(events.map(e => e.type)).toEqual(['thinking', 'tool', 'tool', 'tool', 'action']);
    expect(events.map(e => e.data.name || e.data.action || 'thinking')).toEqual([
      'thinking',
      'run_adb_command',
      'run_adb_command',
      'ask_explorer',
      'wait_for_delay'
    ]);
  });

  it('correctly handles mid-stream reset and supersedes the reset block on retry', () => {
    // 1. Initial stream fails mid-stream and is marked reset
    const resetLogs = [
      {
        type: 'llm_stream',
        timestamp: '2026-09-03T10:00:01.000Z',
        data: {
          execution_id: 'exec-failed-1',
          stream_type: 'text',
          text: 'Here is the aborted plan...',
          isCompleted: false,
          isReset: true,
          resetMessage: 'A request error occurred during output generation, typically caused by lower API priority. Retrying automatically...'
        }
      }
    ];

    const initialBlocks = consolidateLogsToBlocks(resetLogs);
    expect(initialBlocks.length).toBe(1);
    expect(initialBlocks[0].data.isReset).toBeTrue();
    expect(initialBlocks[0].data.resetMessage).toBe('A request error occurred during output generation, typically caused by lower API priority. Retrying automatically...');

    // getSortedStepEvents retains the reset state and message
    const resetEvents = getSortedStepEvents(initialBlocks[0].data);
    expect(resetEvents.length).toBe(1);
    expect(resetEvents[0].type).toBe('text');
    expect(resetEvents[0].data.isReset).toBeTrue();

    // 2. Retry stream arrives without step_id (e.g. pre-planning retry)
    const retryLogs = [
      ...resetLogs,
      {
        type: 'llm_stream',
        timestamp: '2026-09-03T10:00:03.000Z',
        data: {
          execution_id: 'exec-retry-2',
          stream_type: 'text',
          text: 'Here is the fresh completed plan.',
          isCompleted: true
        }
      }
    ];

    const updatedBlocks = consolidateLogsToBlocks(retryLogs);
    // Should supersede the reset block rather than creating an orphaned duplicate card
    expect(updatedBlocks.length).toBe(1);
    expect(updatedBlocks[0].data.execution_id).toBe('exec-retry-2');
    expect(updatedBlocks[0].data.isReset).toBeFalse();
    expect(updatedBlocks[0].data.operator_raw_thinking).toBe('Here is the fresh completed plan.');
    expect(updatedBlocks[0].data.isCompleted).toBeTrue();
  });
});

