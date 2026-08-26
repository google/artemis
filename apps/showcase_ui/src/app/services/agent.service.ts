/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

import { Injectable, signal, inject, computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';

import { Session, ModelInfo, TaskQueueItem, AgentStatusResponse } from '../core/models/session.model';
import { StepItemData, StepReplayFrame } from '../core/models/stream.model';
import { extractStepReplayFrames } from '../utils/action-formatter.util';
export type { Session, ModelInfo, TaskQueueItem, AgentStatusResponse, StepItemData, StepReplayFrame };

const SESSION_CACHE_KEY = 'artemis.sessions.v1';

export interface VideoSegment {
  url: string;
  start: number;
  duration: number;
  width: number;
  height: number;
}

export type RecordingPlaybackStatus =
  | 'idle'
  | 'live'
  | 'processing'
  | 'ready'
  | 'failed'
  | 'unavailable';

export interface StartupProgressEvent {
  session_id?: string;
  stage: string;
  message: string;
  timestamp: number;
}

interface SessionVideoResponse {
  session_id: string;
  status?: 'processing' | 'ready' | 'failed' | 'unavailable';
  has_video: boolean;
  video_url: string | null;
  video_segments?: VideoSegment[];
  retry_after_ms?: number;
  message?: string;
}

@Injectable({
  providedIn: 'root'
})
export class AgentService {
  private http = inject(HttpClient);
  private activePauseCardKey: string | null = null;

  // Signals to expose state to components
  private rawSessions = signal<Session[]>([]);
  private pendingQueue = signal<Session[]>([]);
  public activeTasks = signal<any[]>([]);
  // Persistent tracking of active/pending sessions across polling boundaries
  private activeSessionTracking = new Map<string, Session>();

  public sessions = computed(() => {
    const raw = this.rawSessions();
    const pending = this.pendingQueue();
    const status = this.agentStatus();
    const goal = this.runningGoal();
    const runId = this.runningSessionId();
    const activeList = this.activeTasks();

    const sessionMap = new Map<string, Session>();

    // 1. First add raw sessions from DB
    raw.forEach((s) => {
      const isCurrentActive = ((status === 'running' || status === 'paused') && runId === s.session_id)
        || activeList.some(at => at.session_id === s.session_id);
      const isTerminal = s.status === 'completed' || s.status === 'success' || s.status === 'failed' || s.status === 'cancelled';
      let sStatus = s.status;
      if (!isTerminal) {
        const isPending = this.pendingQueue().some(p => p.session_id === s.session_id) && !isCurrentActive;
        sStatus = isCurrentActive ? (status === 'paused' ? 'paused' : 'running') : (isPending ? 'pending' : s.status);
      } else {
        sStatus = (s.status === 'success') ? 'completed' : s.status;
      }
      const activeMatch = activeList.find(at => at.session_id === s.session_id);
      const pendingMatch = this.pendingQueue().find(p => p.session_id === s.session_id);
      let serial = s.device_serial || s.device_id || activeMatch?.device_id || pendingMatch?.device_serial || null;
      if (!serial && s.device_info) {
        try {
          const info = typeof s.device_info === 'string' ? JSON.parse(s.device_info) : s.device_info;
          serial = info?.device_id || info?.device_serial || null;
        } catch {
          // ignore
        }
      }
      const finalSession: Session = {
        ...s,
        status: sStatus,
        device_serial: serial,
        model_info: isCurrentActive && this.activeModel() ? this.activeModel()! : s.model_info
      };
      sessionMap.set(s.session_id, finalSession);
      if (isTerminal) {
        this.activeSessionTracking.delete(s.session_id);
      } else if (sStatus === 'running' || sStatus === 'paused' || sStatus === 'pending') {
        this.activeSessionTracking.set(s.session_id, finalSession);
      }
    });

    // 2. Add pending queue sessions if not yet in raw sessions
    pending.forEach((p) => {
      if (!sessionMap.has(p.session_id)) {
        const isCurrentRunning = ((status === 'running' || status === 'paused') && runId === p.session_id)
          || activeList.some(at => at.session_id === p.session_id);
        const activeMatch = activeList.find(at => at.session_id === p.session_id);
        const finalPending: Session = {
          ...p,
          status: isCurrentRunning ? (status === 'paused' ? 'paused' : 'running') : (p.status || 'pending'),
          device_serial: p.device_serial || p.device_id || activeMatch?.device_id || null
        };
        sessionMap.set(p.session_id, finalPending);
        this.activeSessionTracking.set(p.session_id, finalPending);
      }
    });

    // 3. Ensure all currently active running sessions are present (multi-device & external runs)
    if (activeList.length > 0) {
      activeList.forEach((at) => {
        const sid = at.session_id || `active-${at.device_id}`;
        const existing = sessionMap.get(sid);
        if (!existing) {
          const newSession: Session = {
            session_id: sid,
            initial_goal: at.goal || goal || '',
            start_time: at.acquired_at ? (new Date(at.acquired_at).getTime() / 1000) : (Date.now() / 1000),
            status: 'running',
            model_info: this.activeModel() || undefined,
            device_serial: at.device_id || null
          };
          sessionMap.set(sid, newSession);
          this.activeSessionTracking.set(sid, newSession);
        } else if (existing.status === 'pending') {
          const updated: Session = {
            ...existing,
            status: 'running',
            device_serial: at.device_id || existing.device_serial
          };
          sessionMap.set(sid, updated);
          this.activeSessionTracking.set(sid, updated);
        }
      });
    } else if ((status === 'running' || status === 'paused') && runId && !sessionMap.has(runId)) {
      const activeSession: Session = {
        session_id: runId,
        initial_goal: goal || '',
        start_time: Date.now() / 1000,
        status,
        model_info: this.activeModel() || undefined
      };
      sessionMap.set(runId, activeSession);
      this.activeSessionTracking.set(runId, activeSession);
    }

    // 4. Bridge transient queue-to-running transition gaps
    this.activeSessionTracking.forEach((ts, sid) => {
      if (!sessionMap.has(sid)) {
        sessionMap.set(sid, {
          ...ts,
          status: 'running'
        });
      }
    });

    return Array.from(sessionMap.values()).sort((a, b) => b.start_time - a.start_time);
  });

  public currentSessionId = signal<string | null>(null);
  public sessionLogs = signal<any[]>([]); // Dynamic array of all raw events received
  public isSessionContentLoading = signal<boolean>(false);
  public agentStatus = signal<string>('idle'); // Status of the agent runner process
  public runningSessionId = signal<string | null>(null);
  public runningGoal = signal<string | null>(null);
  public isPaused = signal<boolean>(false);
  public pausedError = signal<string | null>(null);
  public isRetrying = signal<boolean>(false);
  public retryMessage = signal<string | null>(null);
  public activeModel = signal<{ name: string; id: string; provider: string } | null>(null);
  public userPinnedSessionId = signal<string | null>(null); // Pinned session if user explicitly selected a non-running task
  public startupProgressBySession = signal<Record<string, StartupProgressEvent[]>>({});
  private pendingStartupProgress = signal<StartupProgressEvent[]>([]);

  public currentStartupProgress = computed(() => {
    const sessionId = this.currentSessionId();
    if (sessionId) {
      const normalized = String(sessionId).trim().toLowerCase();
      const bySession = this.startupProgressBySession();
      const direct = bySession[sessionId];
      if (direct?.length) return direct;
      const matched = Object.entries(bySession).find(
        ([key]) => key.trim().toLowerCase() === normalized
      );
      if (matched?.[1]?.length) return matched[1];
    }
    return this.pendingStartupProgress();
  });

  // Notes and Tab States
  public currentNotes = signal<Record<string, string>>({});
  public selectedNoteKey = signal<string>('task_plan.md');
  public activeTab = signal<'tasks' | 'notes'>('tasks');

  // Video Replay Floating Window States
  public isVideoWindowOpen = signal<boolean>(false);
  public isVideoMinimized = signal<boolean>(false);
  public activeVideoUrl = signal<string | null>(null);
  public activeVideoSegments = signal<VideoSegment[]>([]);
  public activeVideoTitle = signal<string>('');
  public isVideoLoading = signal<boolean>(false);
  public recordingPlaybackStatus = signal<RecordingPlaybackStatus>('idle');
  public recordingPlaybackMessage = signal<string>('');
  public shouldAutoplayVideo = signal<boolean>(false);
  public videoSeekRequest = signal<{ seconds: number; requestId: number } | null>(null);
  public playerMode = signal<'video' | 'steps'>('video');
  private activeVideoSessionId: string | null = null;
  private videoSeekRequestId = 0;
  private videoRequestGeneration = 0;
  private videoRetryTimer: ReturnType<typeof setTimeout> | null = null;
  private videoWaitStartedAt = 0;

  /**
   * Computed step replay frames from current session logs
   */
  public currentSessionStepFrames = computed<StepReplayFrame[]>(() => {
    return extractStepReplayFrames(this.sessionLogs());
  });

  public hasCurrentSessionStepFrames = computed<boolean>(() => {
    return this.currentSessionStepFrames().length > 0;
  });

  public setPlayerMode(mode: 'video' | 'steps'): void {
    this.playerMode.set(mode);
  }

  public togglePlayerMode(): void {
    this.playerMode.update((mode) => (mode === 'video' ? 'steps' : 'video'));
  }

  /**
   * Clear user-pinned selection so subsequent runs automatically follow active runner
   */
  public clearUserPinnedSession(): void {
    this.userPinnedSessionId.set(null);
  }

  /**
   * Computed active/viewed session
   */
  public currentSession = computed(() => {
    const curId = this.currentSessionId();
    if (!curId) return null;
    return this.sessions().find(s => s.session_id === curId) || null;
  });

  /**
   * Computed whether the currently viewed session is running
   */
  public isCurrentSessionRunning = computed(() => {
    const session = this.currentSession();
    if (session) return session.status === 'running' || session.status === 'paused';
    return this.agentStatus() === 'running' || this.agentStatus() === 'paused';
  });

  /**
   * Computed video URL for the currently viewed session
   */
  public currentSessionVideoUrl = computed(() => {
    const curId = this.currentSessionId();
    if (!curId) return null;
    const session = this.sessions().find(s => s.session_id === curId);
    return session?.video_url || null;
  });

  public currentSessionRecordingStatus = computed(() => {
    const session = this.currentSession();
    const status = session?.recording_status;
    return status === 'recording' || status === 'finalizing' || status === 'processing'
      ? 'processing'
      : status;
  });

  private eventSource: EventSource | null = null;
  private statusInterval: any = null;
  private sessionLoadGeneration = 0;
  private sessionSnapshotRequestId = 0;
  private sessionSnapshotAppliedId = 0;
  private pendingSnapshotRequests = new Set<number>();

  constructor() {
    this.restoreSessionsCache();
    this.fetchSessions();
    this.startStatusPolling();
  }

  /**
   * Check if any task is currently running
   */
  public isRunningTask = computed(() => {
    if (this.agentStatus() === 'running' || this.agentStatus() === 'paused') return true;
    return this.sessions().some(s => s.status === 'running' || s.status === 'paused');
  });

  /**
   * Run a new task by submitting to backend queue
   */
  public runTask(
    goal: string,
    profile: string = 'flash',
    expectedOutput?: string,
    enableOutputter?: boolean
  ): Observable<any> {
    return new Observable((obs) => {
      const submittedEvent: StartupProgressEvent = {
        stage: 'submitting',
        message: 'Submitting the task',
        timestamp: Date.now() / 1000
      };
      this.pendingStartupProgress.set([submittedEvent]);
      const payload: any = { goal, profile };
      if (expectedOutput && expectedOutput.trim()) {
        payload.expected_output = expectedOutput.trim();
      }
      if (enableOutputter !== undefined) {
        payload.enable_outputter = enableOutputter;
      }
      this.clearUserPinnedSession();
      this.http.post<any>('/api/run', payload).subscribe({
        next: (res) => {
          if (res && res.tasks && res.tasks.length > 0) {
            const newSessionId = res.tasks[0].session_id;
            if (newSessionId) {
              this.appendStartupProgress(
                { ...submittedEvent, session_id: newSessionId },
                newSessionId
              );
              this.pendingStartupProgress.set([]);
              const isCurrentlyRunning =
                this.agentStatus() === 'running' ||
                this.sessions().some((s) => s.status === 'running');
              const activeSessionId = this.runningSessionId()
                || this.sessions().find((s) => s.status === 'running' || s.status === 'paused')?.session_id;
              // The status poll can observe the new runner before /api/run
              // returns. In that case it is still the task we just submitted,
              // not an older task that should remain selected.
              if (!isCurrentlyRunning || activeSessionId === newSessionId) {
                this.selectSession(newSessionId, false);
              }
            }
          }
          this.pendingStartupProgress.set([]);
          obs.next(res);
          obs.complete();
        },
        error: (err) => {
          this.pendingStartupProgress.set([]);
          obs.error(err);
        }
      });
    });
  }

  /**
   * Stop the currently running task (and optionally clear queue)
   */
  public stopTask(stopAll: boolean = false): void {
    const stoppingSessionId = this.runningSessionId()
      || this.sessions().find((session) => session.status === 'running' || session.status === 'paused')?.session_id
      || null;

    // Keep the task's terminal state stable while the stop request and the
    // persisted session update cross the network. Clearing the global runner
    // state first used to make the session merger infer "completed" from the
    // still-stale DB row, causing a brief completed -> cancelled flicker.
    this.setSessionStatus(stoppingSessionId, 'cancelled');

    // Apply optimistic updates: only set idle if stopAll is true or pending queue is empty
    if (stopAll || this.pendingQueue().length === 0) {
      this.agentStatus.set('idle');
      this.runningSessionId.set(null);
      this.runningGoal.set(null);
    }
    this.isPaused.set(false);
    this.pausedError.set(null);
    this.isRetrying.set(false);
    if (stopAll) {
      this.pendingQueue.set([]);
    }

    // Mark active streaming / pending logs in current session as finished
    this.sessionLogs.update((logs) =>
      logs.map((l) => {
        if (l.type === 'llm_stream' && !l.data?.isCompleted) {
          return { ...l, data: { ...l.data, isCompleted: true } };
        }
        return l;
      })
    );

    this.http.post<any>(`/api/stop?all=${stopAll}`, {}).subscribe({
      next: () => {
        this.fetchStatus();
        this.fetchSessions();
      },
      error: (err) => {
        console.error('Failed to stop task:', err);
        this.fetchSessions();
        this.fetchStatus();
      }
    });
  }

  /**
   * Resume paused task
   */
  public resumeTask(): void {
    this.http.post<{ status?: string }>('/api/resume', {}).subscribe({
      next: (response) => {
        if (response?.status !== 'resumed') {
          // A stale recovery control must not optimistically change the task
          // back to running when the backend says there is nothing to resume.
          this.fetchStatus();
          return;
        }
        const resumedSessionId = this.runningSessionId();
        this.isPaused.set(false);
        this.pausedError.set(null);
        this.agentStatus.set('running');
        this.setSessionStatus(resumedSessionId, 'running');
        this.fetchStatus();
      },
      error: (err) => {
        console.error('Failed to resume task:', err);
      }
    });
  }

  /**
   * Fetch all past and active sessions from the backend
   */
  public fetchSessions(): void {
    this.http.get<Session[]>('/api/sessions').subscribe({
      next: (data) => {
        this.rawSessions.set(data);
        this.persistSessionsCache(data);
        this.updateModelForCurrentView(data);
        // On initial load, if nothing is selected, not pinned, not running, and sessions exist, select latest
        if (!this.currentSessionId() && !this.userPinnedSessionId() && this.agentStatus() !== 'running' && data.length > 0) {
          this.selectSession(data[0].session_id, false);
        }
      },
      error: (err) => {
        console.error('Failed to fetch sessions from backend:', err);
      }
    });
  }

  /**
   * Delete an individual session / task
   */
  public deleteSession(sessionId: string): Observable<any> {
    // 1. Optimistically update local session state immediately
    this.rawSessions.update((list) => list.filter((s) => s.session_id !== sessionId));
    this.persistSessionsCache(this.rawSessions());
    this.pendingQueue.update((list) => list.filter((s) => s.session_id !== sessionId));

    if (this.userPinnedSessionId() === sessionId) {
      this.userPinnedSessionId.set(null);
    }

    if (this.currentSessionId() === sessionId) {
      this.selectSession('', false);
      if (this.isVideoWindowOpen()) {
        this.closeVideoPlayer();
      }
      const runningId = this.runningSessionId();
      if (runningId && this.agentStatus() === 'running') {
        this.selectSession(runningId, false);
      }
    }

    // 2. Send API request
    return new Observable((obs) => {
      this.http.post<any>(`/api/sessions/${sessionId}/delete`, {}).subscribe({
        next: (res) => {
          this.fetchSessions();
          this.fetchStatus();
          obs.next(res);
          obs.complete();
        },
        error: (err) => {
          console.error(`Failed to delete session ${sessionId}:`, err);
          this.fetchSessions();
          this.fetchStatus();
          obs.error(err);
        }
      });
    });
  }

  /**
   * Clear all sessions, tasks, and history
   */
  public clearAllHistory(): Observable<any> {
    // 1. Optimistically clear all local session state immediately
    this.userPinnedSessionId.set(null);
    this.rawSessions.set([]);
    this.clearSessionsCache();
    this.pendingQueue.set([]);
    this.selectSession('', false);
    if (this.isVideoWindowOpen()) {
      this.closeVideoPlayer();
    }

    // 2. Send API request
    return new Observable((obs) => {
      this.http.post<any>('/api/cleanup', {}).subscribe({
        next: (res) => {
          this.fetchSessions();
          this.fetchStatus();
          obs.next(res);
          obs.complete();
        },
        error: (err) => {
          console.error('Failed to cleanup history:', err);
          this.fetchSessions();
          this.fetchStatus();
          obs.error(err);
        }
      });
    });
  }

  /**
   * Connect to the Server-Sent Events stream for a specific session
   * @param sessionId The ID of the session to select
   * @param isUserAction Whether this selection was explicitly triggered by user interaction
   */
  public selectSession(sessionId: string, isUserAction: boolean = false): void {
    if (isUserAction) {
      if (sessionId && sessionId === this.runningSessionId() && this.agentStatus() === 'running') {
        // User explicitly clicked the currently running task -> follow live runner
        this.userPinnedSessionId.set(null);
      } else if (sessionId) {
        // User explicitly clicked a non-running or historical task -> pin to it
        this.userPinnedSessionId.set(sessionId);
      } else {
        this.userPinnedSessionId.set(null);
      }
    }

    if (!sessionId) {
      this.sessionLoadGeneration++;
      this.pendingSnapshotRequests.clear();
      this.isSessionContentLoading.set(false);
      this.currentSessionId.set(null);
      this.sessionLogs.set([]);
      this.currentNotes.set({});
      if (this.eventSource) {
        this.eventSource.close();
        this.eventSource = null;
      }
      return;
    }

    if (this.currentSessionId() === sessionId) {
      return; // Already selected
    }

    const loadGeneration = ++this.sessionLoadGeneration;
    this.pendingSnapshotRequests.clear();
    this.sessionSnapshotAppliedId = 0;
    this.isSessionContentLoading.set(true);
    this.currentSessionId.set(sessionId);
    this.sessionLogs.set([]); // Reset logs for new session selection
    this.activePauseCardKey = null;
    this.isPaused.set(false);
    this.pausedError.set(null);
    this.fetchNotes(sessionId);
    this.updateModelForCurrentView();

    // If video window is currently open, dynamically sync/refresh video for new session
    if (this.isVideoWindowOpen()) {
      this.openVideoPlayer(sessionId);
    }

    // Clean up previous event stream connection if any
    if (this.eventSource) {
      this.eventSource.close();
      this.eventSource = null;
    }

    console.log(`Connecting to session: ${sessionId}`);
    // Start reading the persisted snapshot immediately. Previously this waited
    // for the SSE `info` event, adding connection latency to every history click.
    this.backfillSessionSteps(sessionId, loadGeneration);

    this.eventSource = new EventSource(`/api/stream/${sessionId}`);

    // Active runs get one reconciliation snapshot after the stream subscribes,
    // closing the snapshot-to-stream gap without delaying the first paint.
    const selectedStatus = this.sessions().find((session) => session.session_id === sessionId)?.status;
    const shouldReconcileAfterSubscribe = selectedStatus === 'running'
      || selectedStatus === 'paused'
      || selectedStatus === 'pending';
    this.eventSource.addEventListener('info', () => {
      if (shouldReconcileAfterSubscribe) {
        this.backfillSessionSteps(sessionId, loadGeneration);
      }
    });

    // Listen to standard SSE keep-alive
    this.eventSource.addEventListener('keep-alive', () => {
      // No-op keep-alive tick
    });

    // Listen to raw events and append them directly to the logs array
    const eventTypes = [
      'llm_stream',
      'trace_recorded',
      'step_recorded',
      'step_updated',
      'background_tasks_updated',
      'task_paused',
      'task_resumed',
      'llm_retrying',
      'startup_progress',
      'session_started',
      'session_ended',
      'recording_ready',
      'recording_failed'
    ];

    eventTypes.forEach((eventType) => {
      this.eventSource?.addEventListener(eventType, (event: MessageEvent) => {
        try {
          const parsedData = JSON.parse(event.data);

          // If the event carries a session_id for a different session, update global states if applicable and ignore
          const evtSessionId = parsedData?.session_id;

          if (eventType === 'recording_ready' || eventType === 'recording_failed') {
            if (
              this.isVideoWindowOpen()
              && evtSessionId
              && String(evtSessionId) === String(this.activeVideoSessionId)
            ) {
              if (eventType === 'recording_ready') {
                this.refreshActiveRecording(true);
              } else {
                this.cancelVideoRetry();
                this.isVideoLoading.set(false);
                this.activeVideoUrl.set(null);
                this.activeVideoSegments.set([]);
                this.recordingPlaybackStatus.set('failed');
                this.recordingPlaybackMessage.set(
                  parsedData?.error || 'Recording finalization failed.'
                );
              }
            }
            return;
          }

          const isDifferentSession = evtSessionId
            && String(evtSessionId).trim().toLowerCase() !== String(sessionId).trim().toLowerCase();

          if (isDifferentSession) {
            if (eventType === 'session_started') {
              this.agentStatus.set('running');
              this.runningSessionId.set(parsedData.session_id);
              if (parsedData?.initial_goal) {
                this.runningGoal.set(parsedData.initial_goal);
              }
              this.fetchSessions();
              // Only auto-select if user has no current session at all
              const currentId = this.currentSessionId();
              if (!currentId && parsedData?.session_id) {
                this.selectSession(parsedData.session_id, false);
              }
            } else if (eventType === 'session_ended') {
              this.applySessionEndedStatus(evtSessionId, parsedData);
              this.activeTasks.update(list => list.filter(at => at.session_id !== evtSessionId));
              const remaining = this.activeTasks();
              if (remaining.length > 0) {
                if (this.runningSessionId() === evtSessionId) {
                  this.runningSessionId.set(remaining[0].session_id);
                  this.runningGoal.set(remaining[0].goal || null);
                }
              } else if (this.pendingQueue().length > 0) {
                const nextPending = this.pendingQueue()[0];
                this.agentStatus.set('running');
                this.runningSessionId.set(nextPending.session_id);
                this.runningGoal.set(nextPending.initial_goal || null);
              } else {
                this.agentStatus.set('idle');
                this.runningSessionId.set(null);
                this.runningGoal.set(null);
              }
              this.fetchStatus();
              this.fetchSessions();
              this.updateModelForCurrentView();
            }
            return;
          }

          // Guard against obsolete subscriptions
          if (this.currentSessionId() !== sessionId) {
            return;
          }

          if (eventType === 'session_started') {
            this.agentStatus.set('running');
            if (parsedData?.session_id) {
              this.runningSessionId.set(parsedData.session_id);
            }
            if (parsedData?.initial_goal) {
              this.runningGoal.set(parsedData.initial_goal);
            }
            this.fetchSessions();
            return;
          }

          if (eventType === 'session_ended') {
            this.applySessionEndedStatus(sessionId, parsedData);
            this.activeTasks.update(list => list.filter(at => at.session_id !== sessionId));
            const remaining = this.activeTasks();
            if (remaining.length > 0) {
              this.agentStatus.set('running');
              this.runningSessionId.set(remaining[0].session_id);
              this.runningGoal.set(remaining[0].goal || null);
            } else if (this.pendingQueue().length > 0) {
              const nextPending = this.pendingQueue()[0];
              this.agentStatus.set('running');
              this.runningSessionId.set(nextPending.session_id);
              this.runningGoal.set(nextPending.initial_goal || null);
            } else {
              this.agentStatus.set('idle');
              this.runningSessionId.set(null);
              this.runningGoal.set(null);
            }
            this.isRetrying.set(false);
            this.isPaused.set(false);
            this.fetchSessions();
            this.fetchStatus();
            this.fetchNotes(sessionId);
            this.updateModelForCurrentView();
            if (
              this.isVideoWindowOpen()
              && this.activeVideoSessionId === sessionId
              && this.recordingPlaybackStatus() === 'live'
            ) {
              this.beginRecordingFinalization(sessionId);
            }
            return;
          }

          if (eventType === 'startup_progress') {
            this.appendStartupProgress(parsedData, sessionId);
            return;
          }

          if (eventType === 'llm_retrying') {
            this.isRetrying.set(true);
            const attempt = Number(parsedData.attempt || 0);
            const max = Number(parsedData.max_retries || 0);
            const delay = Number(parsedData.delay || 0);
            const attemptText = attempt && max ? ` (Attempt ${attempt}/${max})` : '';
            const delayText = delay > 0 ? `; retrying in ${delay.toFixed(2).replace(/\.00$/, '')}s` : '';
            this.retryMessage.set(`AI service is temporarily busy${attemptText}${delayText}...`);
            this.appendLiveLLMRetryTrace(parsedData, sessionId);
            return;
          }

          if (eventType === 'task_paused') {
            this.isPaused.set(true);
            this.isRetrying.set(false);
            this.agentStatus.set('paused');
            this.runningSessionId.set(sessionId);
            this.setSessionStatus(sessionId, 'paused');
            const pauseError = parsedData.error || 'AI call failed';
            this.pausedError.set(pauseError);
            this.appendPausedErrorCard(
              pauseError,
              sessionId,
              parsedData.timestamp,
              parsedData.step_id,
              parsedData
            );
            return;
          }

          if (eventType === 'task_resumed') {
            this.isPaused.set(false);
            this.isRetrying.set(false);
            this.pausedError.set(null);
            if (this.runningSessionId() === sessionId) {
              this.agentStatus.set('running');
              this.setSessionStatus(sessionId, 'running');
            }
            this.activePauseCardKey = null;
            return;
          }

          if (eventType === 'step_updated' || eventType === 'step_recorded' || eventType === 'llm_stream') {
            this.isRetrying.set(false);
          }

          if (eventType === 'llm_stream') {
            const execId = parsedData.execution_id;
            const stepId = parsedData.step_id;
            const chunk = parsedData.chunk;
            const streamType = parsedData.stream_type || 'text';

            this.sessionLogs.update((logs) => {
              // Find if there is an active stream log with same execution_id and stream_type
              const existingIndex = logs.findIndex(
                (l) => l.type === 'llm_stream' && 
                       l.data.execution_id === execId && 
                       (l.data.stream_type || 'text') === streamType
              );

              if (existingIndex > -1) {
                // Append text in place
                const updatedLogs = [...logs];
                const existingLog = updatedLogs[existingIndex];
                updatedLogs[existingIndex] = {
                  ...existingLog,
                  data: {
                    ...existingLog.data,
                    text: existingLog.data.text + chunk,
                    step_id: stepId || existingLog.data.step_id
                  }
                };
                return updatedLogs;
              } else {
                // First mark any other active streams of same stream_type as completed
                const updatedLogs = logs.map((l) => {
                  if (l.type === 'llm_stream' && 
                      (l.data.stream_type || 'text') === streamType && 
                      !l.data.isCompleted) {
                    return {
                      ...l,
                      data: { ...l.data, isCompleted: true }
                    };
                  }
                  return l;
                });

                // Add new stream log
                return [
                  ...updatedLogs,
                  {
                    type: 'llm_stream',
                    timestamp: new Date().toISOString(),
                    data: {
                      execution_id: execId,
                      step_id: stepId,
                      text: chunk,
                      stream_type: streamType,
                      isCompleted: false
                    }
                  }
                ];
              }
            });
          } else {
            // For other event types, mark any active stream as completed
            this.sessionLogs.update((logs) => {
              const updatedLogs = logs.map((l) => {
                if (l.type === 'llm_stream' && !l.data.isCompleted) {
                  return {
                    ...l,
                    data: { ...l.data, isCompleted: true }
                  };
                }
                return l;
              });

              const evtTime = typeof parsedData?.timestamp === 'number'
                ? new Date(parsedData.timestamp * 1000).toISOString()
                : new Date().toISOString();

              const nextLog = {
                type: eventType,
                timestamp: evtTime,
                data: parsedData
              };

              // A retry is published both as trace_recorded and as the
              // purpose-built llm_retrying event. Replace the optimistic live
              // copy when the persisted trace arrives instead of duplicating it.
              if (eventType === 'trace_recorded' && parsedData?.trace_id) {
                const existingTraceIndex = updatedLogs.findIndex((log) =>
                  log.type === 'trace_recorded'
                  && log.data?.trace_id === parsedData.trace_id
                );
                if (existingTraceIndex > -1) {
                  const deduplicatedLogs = [...updatedLogs];
                  deduplicatedLogs[existingTraceIndex] = nextLog;
                  return deduplicatedLogs;
                }
              }

              return [...updatedLogs, nextLog];
            });
          }

          if (eventType === 'trace_recorded' && parsedData) {
            const trName = parsedData.name || '';
            if (['save_note', 'read_note', 'update_note', 'append_note', 'list_notes', 'outputter'].includes(trName.toLowerCase())) {
              const curSessionId = this.currentSessionId();
              if (curSessionId === sessionId) {
                this.fetchNotes(curSessionId);
              }
            }
          }
        } catch (e) {
          console.error(`Failed to parse ${eventType} event data:`, e);
          // Fallback to plain event data if parsing fails
          this.sessionLogs.update((logs) => [
            ...logs,
            {
              type: eventType,
              timestamp: new Date().toISOString(),
              data: event.data
            }
          ]);
        }
      });
    });

    this.eventSource.onerror = (err) => {
      console.error('EventSource connection error:', err);
    };
  }

  /**
   * Load a fresh persisted snapshot without replacing events already received
   * from SSE. Snapshot logs are replaceable, while live logs remain append-only;
   * the stream aggregator merges matching steps and trace IDs within each step.
   */
  private backfillSessionSteps(sessionId: string, loadGeneration = this.sessionLoadGeneration): void {
    const requestId = ++this.sessionSnapshotRequestId;
    this.pendingSnapshotRequests.add(requestId);
    this.http.get<StepItemData[]>(`/api/sessions/${sessionId}/steps`).subscribe({
      next: (steps) => {
        this.pendingSnapshotRequests.delete(requestId);
        if (
          this.currentSessionId() !== sessionId
          || loadGeneration !== this.sessionLoadGeneration
          || requestId < this.sessionSnapshotAppliedId
        ) return;
        this.sessionSnapshotAppliedId = requestId;
        const historicalLogs = steps.map((step) => ({
          type: 'step_updated',
          session_id: sessionId,
          timestamp: new Date((step.timestamp || Date.now() / 1000) * 1000).toISOString(),
          data: step,
          history_snapshot: true
        }));

        this.sessionLogs.update((logs) => [
          ...historicalLogs,
          ...logs.filter((log) => !log.history_snapshot)
        ]);
        this.isSessionContentLoading.set(false);
      },
      error: (err) => {
        this.pendingSnapshotRequests.delete(requestId);
        console.error('Failed to backfill session steps:', err);
        if (
          this.currentSessionId() === sessionId
          && loadGeneration === this.sessionLoadGeneration
          && this.pendingSnapshotRequests.size === 0
        ) {
          this.isSessionContentLoading.set(false);
        }
      }
    });
  }

  private restoreSessionsCache(): void {
    try {
      const cached = localStorage.getItem(SESSION_CACHE_KEY);
      if (!cached) return;
      const sessions = JSON.parse(cached);
      if (Array.isArray(sessions)) {
        this.rawSessions.set(sessions);
      }
    } catch {
      this.clearSessionsCache();
    }
  }

  private persistSessionsCache(sessions: Session[]): void {
    try {
      localStorage.setItem(SESSION_CACHE_KEY, JSON.stringify(sessions));
    } catch {
      // Storage can be unavailable in private browsing or embedded contexts.
    }
  }

  private clearSessionsCache(): void {
    try {
      localStorage.removeItem(SESSION_CACHE_KEY);
    } catch {
      // No-op when browser storage is unavailable.
    }
  }

  /**
   * Convert the dedicated live retry event into the same trace shape used by
   * history APIs. This keeps live streaming and post-refresh rendering on one
   * data contract instead of maintaining a separate retry UI path.
   */
  private appendLiveLLMRetryTrace(data: any, sessionId: string): void {
    const timestampSeconds = typeof data?.timestamp === 'number'
      ? (data.timestamp > 1e11 ? data.timestamp / 1000 : data.timestamp)
      : Date.now() / 1000;
    const traceId = data?.trace_id
      || `llm-retry-live-${data?.request_id || 'unknown'}-${timestampSeconds}`;
    const payload = {
      error: data?.error,
      delay: data?.delay,
      attempt: data?.attempt,
      max_retries: data?.max_retries,
      provider: data?.provider,
      source: data?.source,
      recoverable: data?.recoverable,
      request_id: data?.request_id,
      scheduled_at: data?.scheduled_at ?? timestampSeconds
    };
    const retryTrace = {
      trace_id: traceId,
      session_id: sessionId,
      step_id: data?.step_id || null,
      type: 'llm_call',
      name: 'llm_retry',
      timestamp: timestampSeconds,
      status: 'retrying',
      payload
    };

    this.sessionLogs.update((logs) => {
      const existingIndex = logs.findIndex((log) =>
        log.type === 'trace_recorded'
        && (
          log.data?.trace_id === traceId
          || (
            data?.request_id
            && log.data?.payload?.request_id === data.request_id
            && Number(log.data?.payload?.scheduled_at) === Number(payload.scheduled_at)
          )
        )
      );
      const retryLog = {
        type: 'trace_recorded',
        session_id: sessionId,
        timestamp: new Date(timestampSeconds * 1000).toISOString(),
        data: retryTrace
      };
      if (existingIndex < 0) return [...logs, retryLog];

      const updatedLogs = [...logs];
      updatedLogs[existingIndex] = retryLog;
      return updatedLogs;
    });
  }

  /**
   * Preserve pause failures in the normal failed-LLM card stream. New runners
   * emit a persisted failed trace first; this fallback also supports older
   * runners whose task_paused event only carried an error string.
   */
  private appendPausedErrorCard(
    error: unknown,
    sessionId: string,
    timestamp?: number | string,
    stepId?: string | null,
    details?: any
  ): void {
    const errorText = typeof error === 'string' ? error : JSON.stringify(error);
    const pauseKey = `${sessionId}:${errorText}`;
    if (this.activePauseCardKey === pauseKey) return;

    this.sessionLogs.update((logs) => {
      const alreadyRecorded = logs.some((log) => {
        const traces = log?.type === 'trace_recorded'
          ? [log.data]
          : (Array.isArray(log?.data?.generic_tools) ? log.data.generic_tools : []);
        return traces.some((trace: any) =>
          trace?.type === 'llm_call'
          && trace?.status === 'failed'
          && String(trace?.payload?.error ?? trace?.error ?? '') === errorText
        );
      });
      if (alreadyRecorded) return logs;

      const latestStepId = stepId || [...logs].reverse().find((log) =>
        (log.type === 'step_updated' || log.type === 'step_recorded') && log.data?.step_id
      )?.data?.step_id || null;
      const timestampMs = typeof timestamp === 'number'
        ? (timestamp < 1e11 ? timestamp * 1000 : timestamp)
        : (timestamp ? new Date(timestamp).getTime() : Date.now());

      return [
        ...logs,
        {
          type: 'trace_recorded',
          session_id: sessionId,
          timestamp: new Date(timestampMs).toISOString(),
          data: {
            trace_id: `task-paused-${sessionId}-${timestampMs}`,
            session_id: sessionId,
            step_id: latestStepId,
            type: 'llm_call',
            name: 'llm_pause',
            status: 'failed',
            timestamp: timestampMs / 1000,
            payload: {
              error: errorText,
              pause: true,
              request_id: details?.request_id,
              provider: details?.provider,
              waited_seconds: details?.waited_seconds,
              retries: Array.isArray(details?.retries) ? details.retries : []
            }
          }
        }
      ];
    });
    this.activePauseCardKey = pauseKey;
  }

  private pollCounter: number = 0;

  private setSessionStatus(sessionId: string | null, status: Session['status']): void {
    if (!sessionId) return;

    this.rawSessions.update((sessions) => sessions.map((session) =>
      session.session_id === sessionId ? { ...session, status } : session
    ));
    this.pendingQueue.update((sessions) => sessions.map((session) =>
      session.session_id === sessionId ? { ...session, status } : session
    ));
  }

  private applySessionEndedStatus(sessionId: unknown, data: any): void {
    if (!sessionId) return;

    const reportedStatus = String(data?.status || '').toLowerCase();
    const status: Session['status'] | null = data?.was_stopped_manually || reportedStatus === 'cancelled'
      ? 'cancelled'
      : reportedStatus === 'failed'
        ? 'failed'
        : reportedStatus === 'completed' || reportedStatus === 'success'
          ? 'completed'
          : null;

    if (status) {
      this.activeSessionTracking.delete(String(sessionId));
      this.setSessionStatus(String(sessionId), status);
      this.activeTasks.update((list) => list.filter((at) => at.session_id !== String(sessionId)));
    }
  }

  /**
   * Start periodic status polling from the backend
   */
  private startStatusPolling(): void {
    this.fetchStatus();
    this.statusInterval = setInterval(() => {
      this.fetchStatus();
      this.pollCounter++;
      // Periodically refresh sessions every 6 seconds (every 3 polling intervals of 2s) to stay in sync with external DB modifications
      if (this.pollCounter % 3 === 0) {
        this.fetchSessions();
      }
    }, 2000);
  }

  /**
   * Fetch current agent runner process status
   */
  public fetchStatus(): void {
    this.http.get<any>('/api/status').subscribe({
      next: (data) => {
        if (data && data.status) {
          const oldStatus = this.agentStatus();
          const oldRunningSessionId = this.runningSessionId();
          const isActive = data.status === 'running' || data.status === 'paused';
          this.agentStatus.set(data.status);
          this.runningSessionId.set(data.session_id || null);
          this.runningGoal.set(data.goal || null);
          if (data.model_info) {
            this.activeModel.set(data.model_info);
          }
          this.isPaused.set(data.status === 'paused');

          if (data.status === 'paused') {
            this.isRetrying.set(false);
            const pauseError = data.paused_error
              || this.pausedError()
              || 'AI model request failed. The task is paused.';
            this.pausedError.set(pauseError);
            if (data.session_id && this.currentSessionId() === data.session_id) {
              this.appendPausedErrorCard(pauseError, data.session_id);
            }
          } else {
            this.pausedError.set(null);
            this.activePauseCardKey = null;
          }

          // Map backend pending queue to real Session / TaskQueueItem objects
          const pending: Session[] = (data.queue || []).map((item: any, index: number) => {
            if (typeof item === 'object' && item !== null) {
              return {
                session_id: item.session_id || `pending-task-${index}`,
                initial_goal: item.goal || '',
                start_time: item.start_time || item.created_at || (Date.now() / 1000 + index),
                status: item.status || 'pending',
                device_serial: item.device_serial || item.device_id || null
              };
            }
            return {
              session_id: `task-queued-${index}`,
              initial_goal: String(item),
              start_time: (Date.now() / 1000) + index,
              status: 'pending'
            };
          });
          this.pendingQueue.set(pending);
          this.activeTasks.set(data.active_tasks || []);
          
          if (oldStatus !== data.status || oldRunningSessionId !== data.session_id) {
            this.fetchSessions();
          }

          if (
            !isActive
            && (oldStatus === 'running' || oldStatus === 'paused')
            && oldRunningSessionId
            && this.isVideoWindowOpen()
            && this.activeVideoSessionId === oldRunningSessionId
            && this.recordingPlaybackStatus() === 'live'
          ) {
            this.beginRecordingFinalization(oldRunningSessionId);
          }

          // Auto-select the session ONLY if nothing is currently selected
          if (isActive && data.session_id) {
            const currentId = this.currentSessionId();
            if (!currentId) {
              this.selectSession(data.session_id, false);
            }
          }
        }
      },
      error: (err) => {
        console.error('Failed to fetch status from backend:', err);
        this.agentStatus.set('offline');
        this.runningSessionId.set(null);
        this.runningGoal.set(null);
      }
    });
  }

  /**
   * Update active model badge according to currently viewed session
   */
  private updateModelForCurrentView(sessionsList?: Session[]): void {
    const curId = this.currentSessionId();
    const allSessions = sessionsList || this.sessions();

    if (curId) {
      const selected = allSessions.find(s => s.session_id === curId);
      if (selected && selected.model_info) {
        this.activeModel.set(selected.model_info);
        return;
      }
    }
  }

  /**
   * Clean up event source connection and timers on service destruction
   */
  public destroy(): void {
    if (this.statusInterval) {
      clearInterval(this.statusInterval);
    }
    if (this.eventSource) {
      this.eventSource.close();
    }
    this.cancelVideoRetry();
  }

  /**
   * Fetch notes for a specific session
   */
  public fetchNotes(sessionId: string): void {
    if (!sessionId) {
      this.currentNotes.set({});
      return;
    }
    this.http.get<any>(`/api/sessions/${sessionId}/notes`).subscribe({
      next: (res) => {
        if (res && res.notes) {
          this.currentNotes.set(res.notes);
          
          // Default to task_plan.md if available, otherwise first note
          const keys = Object.keys(res.notes);
          if (keys.length > 0) {
            const currentSelected = this.selectedNoteKey();
            if (!keys.includes(currentSelected)) {
              if (keys.includes('task_plan.md')) {
                this.selectedNoteKey.set('task_plan.md');
              } else {
                this.selectedNoteKey.set(keys[0]);
              }
            }
          }
        }
      },
      error: (err) => {
        console.error(`Failed to fetch notes for session ${sessionId}:`, err);
      }
    });
  }

  /**
   * Open the video player for a given session or the current session
   */
  public openVideoPlayer(
    sessionId?: string,
    videoUrl?: string,
    title?: string,
    seekSeconds?: number
  ): void {
    const targetSessionId = sessionId || this.currentSessionId();
    const session = this.sessions().find(s => s.session_id === targetSessionId);
    const targetUrl = videoUrl || session?.video_url || null;
    const goalTitle = title || session?.initial_goal || (targetSessionId ? `Task: ${targetSessionId.slice(0, 8)}...` : 'Screen Recording');

    this.activeVideoTitle.set(goalTitle);
    this.isVideoWindowOpen.set(true);
    this.isVideoMinimized.set(false);
    this.activeVideoSessionId = targetSessionId;
    this.cancelVideoRetry();
    this.videoRequestGeneration++;
    this.shouldAutoplayVideo.set(false);
    this.recordingPlaybackMessage.set('');
    this.activeVideoSegments.set([]);
    if (Number.isFinite(seekSeconds)) this.requestVideoSeek(Number(seekSeconds));
    if (!targetSessionId) {
      this.activeVideoUrl.set(null);
      this.isVideoLoading.set(false);
      this.recordingPlaybackStatus.set('unavailable');
      return;
    }

    if (session?.status === 'running' || session?.status === 'paused') {
      this.activeVideoUrl.set(null);
      this.isVideoLoading.set(false);
      this.recordingPlaybackStatus.set('live');
      return;
    }

    this.activeVideoUrl.set(targetUrl);
    this.shouldAutoplayVideo.set(true);
    this.videoWaitStartedAt = Date.now();
    this.isVideoLoading.set(true);
    this.recordingPlaybackStatus.set('processing');
    this.recordingPlaybackMessage.set('Loading screen recording...');
    if (targetUrl) {
      this.playerMode?.set('video');
    } else if (this.hasCurrentSessionStepFrames?.()) {
      this.playerMode?.set('steps');
    } else {
      this.playerMode?.set('video');
    }
    this.requestSessionVideo(targetSessionId, this.videoRequestGeneration);
  }

  private appendStartupProgress(data: any, sessionId: string): void {
    if (!data?.stage || !data?.message) return;

    const timestamp = typeof data.timestamp === 'number'
      ? (data.timestamp > 1e11 ? data.timestamp / 1000 : data.timestamp)
      : Date.now() / 1000;
    const event: StartupProgressEvent = {
      session_id: String(data.session_id || sessionId),
      stage: String(data.stage),
      message: String(data.message),
      timestamp
    };

    const rawKey = String(data.session_id || sessionId).trim();
    this.startupProgressBySession.update((allEvents) => {
      const current = [...(allEvents[rawKey] || allEvents[sessionId] || [])];
      const existingIndex = current.findIndex((item) => item.stage === event.stage);
      if (existingIndex >= 0) {
        current[existingIndex] = event;
      } else {
        current.push(event);
      }
      current.sort((a, b) => a.timestamp - b.timestamp);
      const updated = { ...allEvents, [rawKey]: current };
      if (sessionId && sessionId !== rawKey) {
        updated[sessionId] = current;
      }
      return updated;
    });
  }

  private beginRecordingFinalization(sessionId: string): void {
    if (this.activeVideoSessionId !== sessionId) return;
    this.cancelVideoRetry();
    this.videoRequestGeneration++;
    this.videoWaitStartedAt = Date.now();
    this.activeVideoUrl.set(null);
    this.activeVideoSegments.set([]);
    this.shouldAutoplayVideo.set(true);
    this.isVideoLoading.set(true);
    this.recordingPlaybackStatus.set('processing');
    this.recordingPlaybackMessage.set('Finalizing screen recording...');
    this.requestSessionVideo(sessionId, this.videoRequestGeneration);
  }

  private requestSessionVideo(sessionId: string, generation: number): void {
    this.http.get<SessionVideoResponse>(`/api/sessions/${sessionId}/video`).subscribe({
      next: (res) => {
        if (generation !== this.videoRequestGeneration || sessionId !== this.activeVideoSessionId) {
          return;
        }
        const status = res.status || (res.has_video && res.video_url ? 'ready' : 'unavailable');
        if (status === 'ready' && res.video_url) {
          this.cancelVideoRetry();
          this.isVideoLoading.set(false);
          this.recordingPlaybackStatus.set('ready');
          this.recordingPlaybackMessage.set('');
          this.activeVideoUrl.set(res.video_url);
          this.activeVideoSegments.set(res.video_segments || []);
          this.playerMode?.set('video');
          this.rawSessions.update((list) =>
            list.map((s) => s.session_id === sessionId
              ? { ...s, video_url: res.video_url || undefined, recording_status: 'ready' }
              : s)
          );
          return;
        }
        if (status === 'processing') {
          this.activeVideoUrl.set(null);
          this.activeVideoSegments.set([]);
          this.isVideoLoading.set(true);
          this.recordingPlaybackStatus.set('processing');
          this.recordingPlaybackMessage.set('Finalizing screen recording...');
          this.scheduleVideoRetry(sessionId, generation, res.retry_after_ms);
          return;
        }

        this.cancelVideoRetry();
        this.isVideoLoading.set(false);
        this.activeVideoUrl.set(null);
        this.activeVideoSegments.set([]);
        this.recordingPlaybackStatus.set(status === 'failed' ? 'failed' : 'unavailable');
        this.recordingPlaybackMessage.set(
          res.message || (status === 'failed'
            ? 'Recording finalization failed.'
            : 'No screen recording is available for this task.')
        );
        if (this.hasCurrentSessionStepFrames?.()) {
          this.playerMode?.set('steps');
        }
      },
      error: () => {
        if (generation !== this.videoRequestGeneration || sessionId !== this.activeVideoSessionId) {
          return;
        }
        if (this.recordingPlaybackStatus() === 'processing') {
          this.scheduleVideoRetry(sessionId, generation, 1000);
          return;
        }
        this.isVideoLoading.set(false);
        this.recordingPlaybackStatus.set('failed');
        this.recordingPlaybackMessage.set('Unable to load the screen recording.');
        if (this.hasCurrentSessionStepFrames?.()) {
          this.playerMode?.set('steps');
        }
      }
    });
  }

  private scheduleVideoRetry(sessionId: string, generation: number, retryAfterMs = 1000): void {
    this.cancelVideoRetry();
    if (Date.now() - this.videoWaitStartedAt > 120_000) {
      this.isVideoLoading.set(false);
      this.recordingPlaybackStatus.set('failed');
      this.recordingPlaybackMessage.set('Recording finalization timed out. You can retry.');
      return;
    }
    const delay = Math.max(500, Math.min(3000, retryAfterMs));
    this.videoRetryTimer = setTimeout(() => {
      this.videoRetryTimer = null;
      this.requestSessionVideo(sessionId, generation);
    }, delay);
  }

  private cancelVideoRetry(): void {
    if (this.videoRetryTimer) {
      clearTimeout(this.videoRetryTimer);
      this.videoRetryTimer = null;
    }
  }

  private refreshActiveRecording(autoplay: boolean): void {
    const sessionId = this.activeVideoSessionId;
    if (!sessionId) return;
    this.cancelVideoRetry();
    this.videoRequestGeneration++;
    if (autoplay) this.shouldAutoplayVideo.set(true);
    this.requestSessionVideo(sessionId, this.videoRequestGeneration);
  }

  public retryVideoRecording(): void {
    const sessionId = this.activeVideoSessionId;
    if (!sessionId) return;
    this.beginRecordingFinalization(sessionId);
  }

  public consumeVideoAutoplay(): boolean {
    const shouldAutoplay = this.shouldAutoplayVideo();
    this.shouldAutoplayVideo.set(false);
    return shouldAutoplay;
  }

  public requestVideoSeek(seconds: number): void {
    if (!Number.isFinite(seconds)) return;
    this.videoSeekRequest.set({
      seconds: Math.max(0, seconds),
      requestId: ++this.videoSeekRequestId
    });
  }

  /**
   * Toggle the video player for the current session
   */
  public toggleVideoPlayer(): void {
    if (this.isVideoWindowOpen()) {
      this.isVideoWindowOpen.set(false);
    } else {
      this.openVideoPlayer();
    }
  }

  /**
   * Close the video player
   */
  public closeVideoPlayer(): void {
    this.isVideoWindowOpen.set(false);
    this.cancelVideoRetry();
    this.videoRequestGeneration++;
    this.activeVideoSessionId = null;
  }
}
