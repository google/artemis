import { signal } from '@angular/core';
import { of } from 'rxjs';

import { AgentService } from './agent.service';

describe('AgentService live LLM retry timeline', () => {
  function createServiceWithoutPolling(): AgentService {
    const service = Object.create(AgentService.prototype) as AgentService;
    service.sessionLogs = signal<any[]>([]);
    service.startupProgressBySession = signal({});
    (service as any).pendingStartupProgress = signal<any[]>([]);
    return service;
  }

  it('orders startup milestones and replaces duplicate stages', () => {
    const service = createServiceWithoutPolling();

    (service as any).appendStartupProgress({
      session_id: 'session-1',
      stage: 'device',
      message: 'Checking device',
      timestamp: 102
    }, 'session-1');
    (service as any).appendStartupProgress({
      session_id: 'session-1',
      stage: 'queued',
      message: 'Task queued',
      timestamp: 100
    }, 'session-1');
    (service as any).appendStartupProgress({
      session_id: 'session-1',
      stage: 'device',
      message: 'Device connected',
      timestamp: 103
    }, 'session-1');

    const events = service.startupProgressBySession()['session-1'];
    expect(events.map((event) => event.stage)).toEqual(['queued', 'device']);
    expect(events[1].message).toBe('Device connected');
  });

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

  it('keeps the paused state when the backend says there is nothing to resume', () => {
    const service = createServiceWithoutPolling();
    (service as any).http = { post: () => of({ status: 'not_paused' }) };
    (service as any).rawSessions = signal<any[]>([{ session_id: 'session-1', status: 'paused' }]);
    (service as any).pendingQueue = signal<any[]>([]);
    service.agentStatus = signal('paused');
    service.runningSessionId = signal<string | null>('session-1');
    service.isPaused = signal(true);
    service.pausedError = signal<string | null>('503 unavailable');
    const statusSpy = spyOn(service, 'fetchStatus');

    service.resumeTask();

    expect(service.agentStatus()).toBe('paused');
    expect(service.isPaused()).toBeTrue();
    expect(statusSpy).toHaveBeenCalled();
  });

  it('moves the active session to running only after resume succeeds', () => {
    const service = createServiceWithoutPolling();
    (service as any).http = { post: () => of({ status: 'resumed' }) };
    (service as any).rawSessions = signal<any[]>([{ session_id: 'session-1', status: 'paused' }]);
    (service as any).pendingQueue = signal<any[]>([]);
    service.agentStatus = signal('paused');
    service.runningSessionId = signal<string | null>('session-1');
    service.isPaused = signal(true);
    service.pausedError = signal<string | null>('503 unavailable');
    spyOn(service, 'fetchStatus');

    service.resumeTask();

    expect(service.agentStatus()).toBe('running');
    expect(service.isPaused()).toBeFalse();
    expect((service as any).rawSessions()[0].status).toBe('running');
  });
});

describe('AgentService recording finalization lifecycle', () => {
  function createVideoService(response: any): AgentService {
    const service = Object.create(AgentService.prototype) as AgentService;
    (service as any).http = { get: () => of(response) };
    (service as any).rawSessions = signal<any[]>([
      { session_id: 'session-1', status: 'completed', recording_status: 'recording' }
    ]);
    (service as any).activeVideoSessionId = 'session-1';
    (service as any).videoRequestGeneration = 1;
    (service as any).videoRetryTimer = null;
    (service as any).videoWaitStartedAt = Date.now();
    service.activeVideoUrl = signal<string | null>(null);
    service.activeVideoSegments = signal<any[]>([]);
    service.isVideoLoading = signal(false);
    service.recordingPlaybackStatus = signal('idle');
    service.recordingPlaybackMessage = signal('');
    service.shouldAutoplayVideo = signal(true);
    return service;
  }

  it('keeps unfinished media out of the video element and schedules another readiness check', () => {
    const service = createVideoService({
      session_id: 'session-1',
      status: 'processing',
      has_video: false,
      video_url: null,
      retry_after_ms: 750
    });
    const retrySpy = spyOn<any>(service, 'scheduleVideoRetry');

    (service as any).requestSessionVideo('session-1', 1);

    expect(service.recordingPlaybackStatus()).toBe('processing');
    expect(service.isVideoLoading()).toBeTrue();
    expect(service.activeVideoUrl()).toBeNull();
    expect(retrySpy).toHaveBeenCalledWith('session-1', 1, 750);
  });

  it('publishes finalized media and preserves the automatic replay request', () => {
    const service = createVideoService({
      session_id: 'session-1',
      status: 'ready',
      has_video: true,
      video_url: '/videos/recording.mp4?v=1',
      video_segments: [
        { url: '/videos/recording.mp4?v=1', start: 0, duration: 5, width: 1080, height: 1920 }
      ]
    });

    (service as any).requestSessionVideo('session-1', 1);

    expect(service.recordingPlaybackStatus()).toBe('ready');
    expect(service.isVideoLoading()).toBeFalse();
    expect(service.activeVideoUrl()).toBe('/videos/recording.mp4?v=1');
    expect(service.activeVideoSegments().length).toBe(1);
    expect(service.shouldAutoplayVideo()).toBeTrue();
  });

  it('stops polling and exposes a terminal recording failure', () => {
    const service = createVideoService({
      session_id: 'session-1',
      status: 'failed',
      has_video: false,
      video_url: null,
      message: 'ffmpeg failed'
    });

    (service as any).requestSessionVideo('session-1', 1);

    expect(service.recordingPlaybackStatus()).toBe('failed');
    expect(service.isVideoLoading()).toBeFalse();
    expect(service.recordingPlaybackMessage()).toBe('ffmpeg failed');
  });
});
