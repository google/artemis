import { getUniqueGenericTools } from './tool-formatter.util';

describe('getUniqueGenericTools retry aggregation', () => {
  it('groups recoverable LLM retries while preserving every attempt', () => {
    const tools = [
      {
        trace_id: 'retry-1',
        type: 'llm_call',
        name: 'llm_retry',
        status: 'retrying',
        timestamp: 100,
        payload: { error: '503 first', delay: 1.18, provider: 'google' }
      },
      {
        trace_id: 'retry-2',
        type: 'llm_call',
        name: 'llm_retry',
        status: 'retrying',
        timestamp: 101,
        payload: { error: '503 second', delay: 2.96, provider: 'google' }
      }
    ];

    const result = getUniqueGenericTools(tools);

    expect(result.length).toBe(1);
    expect(result[0].name).toBe('llm_retry_group');
    expect(result[0].payload.retry_count).toBe(2);
    expect(result[0].payload.total_delay).toBeCloseTo(4.14);
    expect(result[0].payload.providers).toEqual(['google']);
    expect(result[0].payload.retries.map((retry: any) => retry.error)).toEqual([
      '503 first',
      '503 second'
    ]);
  });

  it('keeps a terminal LLM failure separate from its preceding retries', () => {
    const result = getUniqueGenericTools([
      {
        trace_id: 'retry-1',
        type: 'llm_call',
        name: 'llm_retry',
        status: 'retrying',
        payload: { error: '503', delay: 1 }
      },
      {
        trace_id: 'failure-1',
        type: 'llm_call',
        name: 'llm_pause',
        status: 'failed',
        payload: { error: '503 exhausted', pause: true }
      }
    ]);

    expect(result.map(tool => tool.name)).toEqual(['llm_retry_group', 'llm_pause']);
  });
});
