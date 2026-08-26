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
