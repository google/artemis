import {
  cleanErrorMessage,
  getCompressionLabel,
  getToolDisplayLabel,
  getToolIcon,
  getUniqueGenericTools,
  getVideoAnalysisView,
  shouldShowTool
} from './tool-formatter.util';

describe('compress_history timeline line', () => {
  const trace = (status: string, args: Record<string, any>) => ({
    type: 'tool',
    name: 'compress_history',
    status,
    payload: { args }
  });

  it('is shown as a plain tool line with its own icon', () => {
    const running = trace('running', { start_step: 12, end_step: 27 });
    expect(shouldShowTool(running)).toBeTrue();
    expect(getToolIcon(running)).toBe('compress');
    expect(getToolDisplayLabel(running)).toBe(getCompressionLabel(running));
  });

  it('says which steps are being condensed while running', () => {
    expect(getCompressionLabel(trace('running', { start_step: 12, end_step: 27 })))
      .toBe('Condensing steps 12–27 into a short memory to free up room…');
    expect(getCompressionLabel(trace('running', { start_step: 12, end_step: 27, note: 'retrying' })))
      .toBe('Retrying the memory summary for steps 12–27…');
    expect(getCompressionLabel(trace('running', { start_step: 5, end_step: 5 })))
      .toBe('Condensing step 5 into a short memory to free up room…');
  });

  it('reports the size reduction and the working memory once done', () => {
    const done = trace('success', {
      start_step: 12,
      end_step: 27,
      source_tokens: 8400,
      summary_tokens: 600,
      context_tokens: 54000,
      context_budget: 80000
    });
    expect(getCompressionLabel(done)).toBe(
      'Steps 12–27 condensed into a short memory · 8.4k → 600 tokens (14× smaller) · working memory ≈ 54k of 80k tokens'
    );
  });

  it('omits the working memory figure on lines that do not carry it', () => {
    const done = trace('success', { start_step: 1, end_step: 4, source_tokens: 3000, summary_tokens: 1500 });
    expect(getCompressionLabel(done)).toBe('Steps 1–4 condensed into a short memory · 3k → 1.5k tokens (2× smaller)');
  });

  it('explains a forced snapshot and a failed attempt in plain words', () => {
    const forced = trace('success', { start_step: 1, end_step: 4, forced: true, context_tokens: 70000, context_budget: 80000 });
    expect(getCompressionLabel(forced))
      .toBe('Steps 1–4 replaced by a brief snapshot (memory was nearly full) · working memory ≈ 70k of 80k tokens');
    expect(getCompressionLabel(trace('failed', { start_step: 1, end_step: 4 })))
      .toBe("Couldn't condense steps 1–4 yet; keeping the full record and retrying later");
  });
});

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

describe('shouldShowTool (Option A Single Source of Truth)', () => {
  it('keeps ADB commands visible even when stepData.action_taken is present', () => {
    const adbTool = {
      name: 'run_adb_command',
      type: 'tool',
      payload: { CommandLine: 'dumpsys telephony.registry' }
    };
    const stepData = {
      action_taken: [{ action: 'wait_for_delay', time_in_ms: 1000 }]
    };

    expect(shouldShowTool(adbTool, stepData)).toBeTrue();
  });

  it('keeps explorer calls visible when stepData.action_taken is present', () => {
    const explorerTool = {
      name: 'ask_explorer',
      type: 'tool'
    };
    const stepData = {
      action_taken: [{ action: 'wait_for_delay', time_in_ms: 1000 }]
    };

    expect(shouldShowTool(explorerTool, stepData)).toBeTrue();
  });

  it('filters out internal plumbing tools', () => {
    const plumbingTool = {
      name: 'safety_net_pixel_validation',
      type: 'tool'
    };
    expect(shouldShowTool(plumbingTool)).toBeFalse();
  });
});
