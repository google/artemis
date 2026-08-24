import { cleanErrorMessage, getUniqueGenericTools } from './tool-formatter.util';

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
