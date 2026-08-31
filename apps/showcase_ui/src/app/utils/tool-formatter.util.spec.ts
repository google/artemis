import {
  cleanErrorMessage,
  getUniqueGenericTools,
  getVideoAnalysisView
} from './tool-formatter.util';

describe('cleanErrorMessage', () => {
  it('removes repeated LLM wrapper labels while preserving the provider reason', () => {
    expect(cleanErrorMessage('LLM Error: LLM Request Error: 503 model overloaded'))
      .toBe('503 model overloaded');
  });

  it('does not present an empty LLM wrapper label as an error reason', () => {
    expect(cleanErrorMessage('LLM Error:')).toBe('Unknown error');
  });
});

describe('getUniqueGenericTools retry aggregation', () => {
  it('groups recoverable LLM retries while preserving every attempt', () => {
    const tools = [
      {
        trace_id: 'retry-1',
        type: 'llm_call',
        name: 'llm_retry',
        status: 'retrying',
        timestamp: 100,
        payload: { error: '503 first', delay: 1.18, provider: 'google', source: 'provider_sdk', request_id: 'request-1' }
      },
      {
        trace_id: 'retry-2',
        type: 'llm_call',
        name: 'llm_retry',
        status: 'retrying',
        timestamp: 101,
        payload: { error: '503 second', delay: 2.96, provider: 'google', source: 'provider_sdk', request_id: 'request-1' }
      }
    ];

    const result = getUniqueGenericTools(tools);

    expect(result.length).toBe(1);
    expect(result[0].name).toBe('llm_retry_group');
    expect(result[0].payload.retry_count).toBe(2);
    expect(result[0].payload.total_delay).toBeCloseTo(4.14);
    expect(result[0].payload.retries.map((retry: any) => retry.error)).toEqual([
      '503 first',
      '503 second'
    ]);
  });

  it('folds SDK retry rows into their matching terminal failure', () => {
    const result = getUniqueGenericTools([
      {
        trace_id: 'retry-1',
        type: 'llm_call',
        name: 'llm_retry',
        status: 'retrying',
        payload: { error: '503', delay: 1, provider: 'google', source: 'provider_sdk', request_id: 'request-1' }
      },
      {
        trace_id: 'failure-1',
        type: 'llm_call',
        name: 'llm_pause',
        status: 'failed',
        payload: { error: '503 exhausted', pause: true, request_id: 'request-1', retries: [{ delay: 1 }] }
      }
    ]);

    expect(result.map(tool => tool.name)).toEqual(['llm_pause']);
  });

  it('does not expose opaque retries from non-Gemini providers', () => {
    const result = getUniqueGenericTools([
      {
        trace_id: 'retry-openai',
        type: 'llm_call',
        name: 'llm_retry',
        status: 'retrying',
        payload: { error: '429', delay: 2, provider: 'openai', source: 'artemis_wrapper' }
      },
      {
        trace_id: 'failure-openai',
        type: 'llm_call',
        name: 'llm_pause',
        status: 'failed',
        payload: { error: '429 exhausted', pause: true, waited_seconds: 12 }
      }
    ]);

    expect(result.map(tool => tool.name)).toEqual(['llm_pause']);
  });
});

describe('video analysis timeline formatting', () => {
  it('distinguishes a cache hit without presenting it as running', () => {
    const view = getVideoAnalysisView({
      name: 'spawn_sub_agent',
      status: 'success',
      payload: {
        args: { start_time: 0, end_time: 42 },
        result: 'CACHED VIDEO ANALYSIS: existing evidence'
      }
    });

    expect(view?.outcome).toBe('complete');
    expect(view?.reuse).toBe('full');
    expect(view?.title).toBe('Reused video analysis');
    expect(view?.requestedRange).toEqual({ start: 0, end: 42 });
  });

  it('collapses child video chunks into one stable note-style timeline item', () => {
    const result = getUniqueGenericTools([
      {
        trace_id: 'chunk-1',
        parent_trace_id: 'video-agent-1',
        type: 'tool',
        name: 'spawn_sub_agent',
        status: 'success',
        timestamp: 100,
        payload: {
          args: { start_time: 0, end_time: 30, specific_query: 'find the result' },
          result: '[from 0.0s to 30.0s] Summary: first'
        }
      },
      {
        trace_id: 'chunk-2',
        parent_trace_id: 'video-agent-1',
        type: 'tool',
        name: 'spawn_sub_agent',
        status: 'success',
        timestamp: 101,
        payload: {
          args: { start_time: 30, end_time: 60, specific_query: 'find the result' },
          result: 'PARTIAL VIDEO ANALYSIS (successful chunks were persisted). Failed intervals: 45.0s-60.0s'
        }
      }
    ]);

    expect(result.length).toBe(1);
    expect(result[0].name).toBe('video_analysis');
    expect(result[0].trace_id).toBe('video-analysis-video-agent-1');
    expect(result[0].payload.result.outcome).toBe('partial');
    expect(result[0].payload.result.requested_range).toEqual({ start: 0, end: 60 });
    expect(getVideoAnalysisView(result[0])?.title).toBe('Video analysis partially completed');
  });

  it('keeps video analyses from independent parent executions separate', () => {
    const result = getUniqueGenericTools([
      {
        trace_id: 'chunk-1', parent_trace_id: 'video-agent-1', type: 'tool',
        name: 'spawn_sub_agent', status: 'success',
        payload: { args: { start_time: 0, end_time: 10 }, result: 'first' }
      },
      {
        trace_id: 'chunk-2', parent_trace_id: 'video-agent-2', type: 'tool',
        name: 'spawn_sub_agent', status: 'success',
        payload: { args: { start_time: 10, end_time: 20 }, result: 'second' }
      }
    ]);

    expect(result.map(tool => tool.trace_id)).toEqual([
      'video-analysis-video-agent-1',
      'video-analysis-video-agent-2'
    ]);
  });
});
