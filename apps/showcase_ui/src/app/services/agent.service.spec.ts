import { signal } from '@angular/core';
import { of } from 'rxjs';

import { AgentService } from './agent.service';

describe('AgentService live LLM retry timeline', () => {
  function createServiceWithoutPolling(): AgentService {
    const service = Object.create(AgentService.prototype) as AgentService;
    service.sessionLogs = signal<any[]>([]);
    return service;
  }

  it('normalizes a live retry event into the historical trace contract', () => {
    const service = createServiceWithoutPolling();
    const event = {
      trace_id: 'retry-1',
      step_id: 'step-1',
      timestamp: 100,
      error: '503 high demand',
      delay: 1.5,
      provider: 'google',
      source: 'provider_sdk',
      recoverable: true,
      request_id: 'request-1',
      scheduled_at: 100
    };

    (service as any).appendLiveLLMRetryTrace(event, 'session-1');

    const [log] = service.sessionLogs();
    expect(log.type).toBe('trace_recorded');
    expect(log.data).toEqual(jasmine.objectContaining({
      trace_id: 'retry-1',
      session_id: 'session-1',
      step_id: 'step-1',
      type: 'llm_call',
      name: 'llm_retry',
      status: 'retrying'
    }));
    expect(log.data.payload).toEqual(jasmine.objectContaining({
      error: '503 high demand',
      delay: 1.5,
      provider: 'google',
      source: 'provider_sdk',
      request_id: 'request-1'
    }));
  });

  it('upserts duplicate live retry events instead of adding another row', () => {
    const service = createServiceWithoutPolling();
    const event = {
      trace_id: 'retry-1',
      timestamp: 100,
      delay: 1.5,
      provider: 'google',
      source: 'provider_sdk',
      request_id: 'request-1',
      scheduled_at: 100
    };

    (service as any).appendLiveLLMRetryTrace(event, 'session-1');
    (service as any).appendLiveLLMRetryTrace({ ...event, error: 'updated 503' }, 'session-1');

    expect(service.sessionLogs().length).toBe(1);
    expect(service.sessionLogs()[0].data.payload.error).toBe('updated 503');
  });

  it('replaces an old history snapshot while preserving live retry events', () => {
    const service = createServiceWithoutPolling();
    (service as any).currentSessionId = signal<string | null>('session-1');
    (service as any).http = {
      get: () => of([{
        step_id: 'step-1',
        timestamp: 100,
        generic_tools: [{ trace_id: 'persisted-retry', name: 'llm_retry' }]
      }])
    };
    service.sessionLogs.set([
      { type: 'step_updated', history_snapshot: true, data: { step_id: 'old-step' } },
      { type: 'trace_recorded', data: { trace_id: 'live-retry', name: 'llm_retry' } }
    ]);

    (service as any).backfillSessionSteps('session-1');

    const logs = service.sessionLogs();
    expect(logs.length).toBe(2);
    expect(logs[0].history_snapshot).toBeTrue();
    expect(logs[0].data.step_id).toBe('step-1');
    expect(logs[1].data.trace_id).toBe('live-retry');
  });

  it('follows a just-started task even when status polling saw it first', () => {
    const service = createServiceWithoutPolling();
    (service as any).http = {
      post: () => of({ tasks: [{ session_id: 'new-session' }] })
    };
    service.agentStatus = signal('running');
    service.runningSessionId = signal<string | null>('new-session');
    service.userPinnedSessionId = signal<string | null>(null);
    (service as any).sessions = signal<any[]>([{
      session_id: 'new-session',
      status: 'running'
    }]);
    const selectSpy = spyOn(service, 'selectSession');

    service.runTask('test goal').subscribe();

    expect(selectSpy).toHaveBeenCalledWith('new-session', false);
  });
});
