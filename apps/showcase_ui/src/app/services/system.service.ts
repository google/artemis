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

import { Injectable, signal, computed, inject, DestroyRef, NgZone } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, finalize, shareReplay, tap } from 'rxjs';
import {
  AdbServerConnectionResponse,
  AdbServerStatus,
  SystemReadinessReport,
  DeviceInfo,
  ProbeResult,
  EmulatorLaunchState,
  EmulatorLaunchStage
} from '../core/models/system.model';

@Injectable({
  providedIn: 'root'
})
export class SystemService {
  private http = inject(HttpClient);

  // Core reactive signals
  public readinessReport = signal<SystemReadinessReport | null>(null);
  public hasReadinessReport = computed(() => this.readinessReport() !== null);
  public isLoading = signal<boolean>(false);
  public isRestartingAdb = signal<boolean>(false);
  public adbServerStatus = signal<AdbServerStatus | null>(null);
  public isRemoteAdbServer = computed(
    () => this.adbServerStatus()?.endpoint.mode === 'remote'
  );
  public launchingAvd = signal<string | null>(null);
  public emulatorLaunchState = signal<EmulatorLaunchState | null>(null);
  public isEmulatorLaunching = computed(() => {
    const s = this.emulatorLaunchState()?.status;
    return s === 'starting' || s === 'waiting_for_adb' || s === 'booting';
  });
  public lastCheckedTime = signal<Date | null>(null);
  private emulatorPollTimer: any = null;

  // Probe lookups
  public pythonProbe = computed(() => this.probes().find(p => p.id === 'python_runtime') || null);
  public configProbe = computed(() => this.probes().find(p => p.id === 'system_config') || null);
  public toolchainProbe = computed(() => this.probes().find(p => p.id === 'toolchain') || null);
  public adbProbe = computed(() => this.probes().find(p => p.id === 'android_adb') || null);
  public llmProbe = computed(() => this.probes().find(p => p.id === 'gemini_api_key' || p.id === 'llm_api_key') || null);
  public geminiProbe = computed(() => this.llmProbe());
  public ocrProbe = computed(() => this.probes().find(p => p.id === 'vision_ocr_key' || p.id === 'ocr_api_key') || null);

  // Grouped readiness helpers for the 3-step onboarding flow
  public isEnvironmentReady = computed(() => {
    const py = this.pythonProbe();
    const cfg = this.configProbe();
    const adb = this.adbProbe();
    const tc = this.toolchainProbe();
    const pyOk = py?.status === 'pass';
    const cfgOk = cfg?.status === 'pass';
    const adbInstalled = adb?.metadata?.['installed'] ?? (adb?.status !== 'fail');
    const tcOk = tc?.status === 'pass' || tc?.is_blocker === false;
    return pyOk && cfgOk && adbInstalled && tcOk;
  });

  public isSkipCredentialsCheck = signal<boolean>(false);

  public isCredentialsReady = computed(() => {
    if (this.isSkipCredentialsCheck()) {
      return true;
    }
    const llm = this.llmProbe();
    return llm?.status === 'pass';
  });

  public isDeviceReady = computed(() => {
    const adb = this.adbProbe();
    return adb?.status === 'pass';
  });

  // Step-level counting matching the 3-step onboarding guide
  public totalStepCount = computed(() => 3);
  public passedStepCount = computed(() => {
    let count = 0;
    if (this.isEnvironmentReady()) count++;
    if (this.isCredentialsReady()) count++;
    if (this.isDeviceReady()) count++;
    return count;
  });

  // Computed helper signals
  public isReady = computed(() => {
    return this.isEnvironmentReady() && this.isCredentialsReady() && this.isDeviceReady();
  });
  public blockerCount = computed(() => this.totalStepCount());
  public passedBlockerCount = computed(() => this.passedStepCount());
  public probes = computed(() => this.readinessReport()?.probes ?? []);
  public activeDevice = computed(() => this.readinessReport()?.active_device ?? null);
  public osType = computed<'linux' | 'darwin' | 'windows'>(() => {
    const raw = this.readinessReport()?.os_type;
    if (raw === 'windows' || raw === 'win32') return 'windows';
    if (raw === 'darwin' || raw === 'macos' || raw === 'mac') return 'darwin';
    return 'linux';
  });

  public skipCredentialsCheck(): void {
    this.isSkipCredentialsCheck.set(true);
  }

  public setSkipCredentialsCheck(skip: boolean): void {
    this.isSkipCredentialsCheck.set(skip);
  }


  // Device list from metadata
  public connectedDevices = computed<DeviceInfo[]>(() => {
    const meta = this.adbProbe()?.metadata;
    if (meta && Array.isArray(meta['devices'])) {
      return meta['devices'] as DeviceInfo[];
    }
    return [];
  });

  // Installed local AVD emulators from metadata
  public installedAvds = computed<string[]>(() => {
    const meta = this.adbProbe()?.metadata;
    if (meta && Array.isArray(meta['installed_avds'])) {
      return meta['installed_avds'] as string[];
    }
    return [];
  });

  public emulatorPath = computed<string | null>(() => {
    return (this.adbProbe()?.metadata?.['emulator_path'] as string) || null;
  });

  public isEmulatorInPath = computed<boolean>(() => {
    return (this.adbProbe()?.metadata?.['is_emulator_in_path'] as boolean) ?? true;
  });

  private pollingTimer: any = null;
  private readinessRequest$: Observable<SystemReadinessReport> | null = null;
  private zone = inject(NgZone);
  private lastAppliedReportJson: string | null = null;
  private onVisibilityChange = () => {
    if (typeof document !== 'undefined' && !document.hidden) {
      this.fetchReadiness(true).subscribe({ error: () => {} });
    }
  };

  constructor() {
    this.startAutoPolling(3000);
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', this.onVisibilityChange);
    }
    inject(DestroyRef).onDestroy(() => {
      this.stopAutoPolling();
      this.stopEmulatorStatusPolling();
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', this.onVisibilityChange);
      }
    });
  }

  /**
   * Start periodic auto-polling for system readiness changes. The interval
   * runs outside the Angular zone (a tick alone must not trigger change
   * detection) and pauses while the tab is hidden; a visibilitychange listener
   * refreshes immediately when the tab becomes visible again.
   */
  public startAutoPolling(intervalMs: number = 3000): void {
    if (this.pollingTimer) {
      clearInterval(this.pollingTimer);
    }
    this.zone.runOutsideAngular(() => {
      this.pollingTimer = setInterval(() => {
        if (typeof document !== 'undefined' && document.hidden) {
          return;
        }
        // Perform silent background check without disturbing UI loading state
        this.fetchReadiness(true).subscribe({
          error: () => {} // Silent catch
        });
      }, intervalMs);
    });
  }

  /**
   * Stop auto-polling
   */
  public stopAutoPolling(): void {
    if (this.pollingTimer) {
      clearInterval(this.pollingTimer);
      this.pollingTimer = null;
    }
  }

  /**
   * Fetch latest system readiness report from backend
   * @param silent If true, updates signals silently without triggering global isLoading spinner
   */
  public fetchReadiness(
    silent: boolean = false,
    forceRefresh: boolean = false
  ): Observable<SystemReadinessReport> {
    if (!silent) {
      this.isLoading.set(true);
    }

    // Share one request across initialization, focus events, polling, and manual
    // refreshes. This prevents a slow probe from creating an unbounded queue.
    if (this.readinessRequest$) {
      return this.readinessRequest$;
    }

    const request$ = this.http.get<SystemReadinessReport>('/api/system/readiness', {
      params: forceRefresh ? { force: true } : {}
    }).pipe(
      tap({
        next: (report) => {
          this.applyReadinessReport(report);
        },
        error: (err) => {
          console.error('Failed to fetch system readiness:', err);
        }
      }),
      finalize(() => {
        if (this.readinessRequest$ === request$) {
          this.readinessRequest$ = null;
        }
        this.isLoading.set(false);
      }),
      shareReplay({ bufferSize: 1, refCount: false })
    );
    this.readinessRequest$ = request$;
    return request$;
  }

  private applyReadinessReport(report: SystemReadinessReport): void {
    const current = this.readinessReport();
    if (current && report.timestamp < current.timestamp) {
      return;
    }
    this.lastCheckedTime.set(new Date(report.timestamp * 1000));
    // A poll usually returns an identical report; setting a new object anyway
    // would cascade through every probe computed for no visible change.
    const serialized = JSON.stringify({ ...report, timestamp: 0 });
    if (serialized === this.lastAppliedReportJson) {
      return;
    }
    this.lastAppliedReportJson = serialized;
    this.readinessReport.set(report);
  }

  /**
   * Fetch current emulator launch progress state snapshot
   */
  public fetchEmulatorStatus(): Observable<EmulatorLaunchState> {
    return this.http.get<EmulatorLaunchState>('/api/system/emulator/status').pipe(
      tap({
        next: (state) => {
          this.emulatorLaunchState.set(state);
          if (state.status === 'ready') {
            this.launchingAvd.set(null);
            this.stopEmulatorStatusPolling();
            this.fetchReadiness().subscribe();
          } else if (state.status === 'failed' || state.status === 'stopped' || state.status === 'idle') {
            this.launchingAvd.set(null);
            this.stopEmulatorStatusPolling();
          } else {
            if (state.avd_name) {
              this.launchingAvd.set(state.avd_name);
            }
          }
        },
        error: (err) => {
          console.error('Failed to fetch emulator status:', err);
        }
      })
    );
  }

  /**
   * Start 1s interval polling for emulator boot progression
   */
  public startEmulatorStatusPolling(): void {
    if (this.emulatorPollTimer) {
      clearInterval(this.emulatorPollTimer);
    }
    this.emulatorPollTimer = setInterval(() => {
      this.fetchEmulatorStatus().subscribe({
        error: () => {}
      });
    }, 1000);
  }

  /**
   * Stop polling for emulator boot status
   */
  public stopEmulatorStatusPolling(): void {
    if (this.emulatorPollTimer) {
      clearInterval(this.emulatorPollTimer);
      this.emulatorPollTimer = null;
    }
  }

  /**
   * Launch a local AVD emulator in the background and continuously track boot progress
   */
  public launchEmulator(avdName: string): Observable<EmulatorLaunchState> {
    this.launchingAvd.set(avdName);
    this.emulatorLaunchState.set({
      avd_name: avdName,
      status: 'starting',
      pid: null,
      serial: null,
      stage_message: 'Spawning emulator process...',
      progress_percent: 15,
      started_at: Date.now() / 1000,
      elapsed_seconds: 0,
      error: null,
      logs: [`Initiating launch for AVD: ${avdName}...`],
      can_retry: true,
    });

    return this.http.post<EmulatorLaunchState>('/api/system/emulator/launch', { avd_name: avdName }).pipe(
      tap({
        next: (state) => {
          this.emulatorLaunchState.set(state);
          if (state.status === 'failed') {
            this.launchingAvd.set(null);
          } else {
            this.startEmulatorStatusPolling();
          }
        },
        error: (err) => {
          console.error('Failed to launch emulator:', err);
          this.launchingAvd.set(null);
          const errorMsg = err?.error?.detail || err?.message || 'Failed to start emulator process.';
          this.emulatorLaunchState.set({
            avd_name: avdName,
            status: 'failed',
            pid: null,
            serial: null,
            stage_message: 'Failed to initiate launch.',
            progress_percent: 0,
            started_at: null,
            elapsed_seconds: 0,
            error: errorMsg,
            logs: [errorMsg],
            can_retry: true,
          });
        }
      })
    );
  }

  /**
   * Terminate running emulator
   */
  public stopEmulator(): Observable<any> {
    return this.http.post<any>('/api/system/emulator/stop', {}).pipe(
      tap({
        next: () => {
          this.launchingAvd.set(null);
          this.stopEmulatorStatusPolling();
          this.fetchEmulatorStatus().subscribe();
          this.fetchReadiness().subscribe();
        }
      })
    );
  }

  /**
   * Clear and dismiss emulator launch tracker state
   */
  public dismissEmulatorStatus(): Observable<any> {
    return this.http.post<any>('/api/system/emulator/dismiss', {}).pipe(
      tap({
        next: () => {
          this.emulatorLaunchState.set(null);
          this.launchingAvd.set(null);
          this.stopEmulatorStatusPolling();
        }
      })
    );
  }

  /**
   * Restart local ADB server and refresh readiness state
   */
  public restartAdb(): Observable<any> {
    this.isRestartingAdb.set(true);
    return this.http.post<any>('/api/system/adb/restart', {}).pipe(
      tap({
        next: (res) => {
          if (res?.report) {
            this.applyReadinessReport(res.report);
          }
          this.isRestartingAdb.set(false);
        },
        error: (err) => {
          console.error('Failed to restart ADB server:', err);
          this.isRestartingAdb.set(false);
        }
      })
    );
  }

  /**
   * Connect to an Android device over Wi-Fi
   */
  public connectWirelessAdb(host: string, port: number = 5555): Observable<any> {
    return this.http.post<any>('/api/system/adb/connect', { host, port }).pipe(
      tap({
        next: (res) => {
          if (res?.report) {
            this.applyReadinessReport(res.report);
          }
        }
      })
    );
  }

  /**
   * Fetch the process-wide ADB server endpoint used by Artemis.
   */
  public fetchAdbServerStatus(): Observable<AdbServerStatus> {
    return this.http.get<AdbServerStatus>('/api/system/adb/server').pipe(
      tap(status => this.adbServerStatus.set(status))
    );
  }

  /**
   * Test an ADB server endpoint without changing the active connection.
   */
  public probeAdbServer(
    host: string,
    port: number
  ): Observable<AdbServerConnectionResponse> {
    return this.http.post<AdbServerConnectionResponse>(
      '/api/system/adb/server/probe',
      { host, port, persist: false }
    );
  }

  /**
   * Validate and activate an ADB server endpoint.
   */
  public connectAdbServer(
    host: string,
    port: number,
    persist: boolean = true
  ): Observable<AdbServerConnectionResponse> {
    return this.http.post<AdbServerConnectionResponse>(
      '/api/system/adb/server/connect',
      { host, port, persist }
    ).pipe(
      tap(response => {
        if (response.connection_result.success) {
          this.adbServerStatus.set({ endpoint: response.connection_result.endpoint });
          if (response.report) {
            this.applyReadinessReport(response.report);
          }
        }
      })
    );
  }

  /**
   * Switch Artemis back to the standard local ADB server.
   */
  public useLocalAdbServer(persist: boolean = true): Observable<AdbServerConnectionResponse> {
    return this.http.post<AdbServerConnectionResponse>(
      '/api/system/adb/server/local',
      {},
      { params: { persist } }
    ).pipe(
      tap(response => {
        this.adbServerStatus.set({ endpoint: response.connection_result.endpoint });
        if (response.report) {
          this.applyReadinessReport(response.report);
        }
      })
    );
  }

  /**
   * Select a specific connected device serial as active target
   */
  public selectDevice(serial: string): Observable<any> {
    this.isLoading.set(true);
    return this.http.post<any>('/api/system/devices/select', { serial }).pipe(
      tap({
        next: (res) => {
          if (res?.report) {
            this.applyReadinessReport(res.report);
          }
          this.isLoading.set(false);
        },
        error: (err) => {
          console.error('Failed to select active device:', err);
          this.isLoading.set(false);
        }
      })
    );
  }

  public currentApiKey = computed<string>(() => {
    return (this.llmProbe()?.metadata?.['current_key'] as string) || '';
  });

  public apiKeysMap = computed<Record<string, string>>(() => {
    return (this.llmProbe()?.metadata?.['api_keys'] as Record<string, string>) || {};
  });

  public modelConfigEnv = signal<ModelConfigEnvResponse | null>(null);

  /**
   * Fetch current model configuration (artemis.jsonc) and environment (.env) status
   */
  public fetchModelConfigEnv(): Observable<ModelConfigEnvResponse> {
    return this.http.get<ModelConfigEnvResponse>('/api/system/model-config-env').pipe(
      tap({
        next: (data) => {
          this.modelConfigEnv.set(data);
        },
        error: (err) => {
          console.error('Failed to fetch model config and env:', err);
        }
      })
    );
  }

  /**
   * Test and verify an API key against live provider endpoint without persisting
   */
  public testApiKey(provider: string, apiKey: string, baseUrl?: string): Observable<{ valid: boolean; provider: string; message: string }> {
    return this.http.post<{ valid: boolean; provider: string; message: string }>('/api/system/credentials/test', {
      provider,
      api_key: apiKey,
      base_url: baseUrl
    });
  }

  /**
   * Update, verify, and configure API key for an LLM provider or Vision OCR
   */
  public updateApiKey(provider: string, apiKey: string, persistToEnv: boolean = true): Observable<any> {
    return this.http.post<any>('/api/system/credentials', {
      provider,
      api_key: apiKey,
      persist_to_env: persistToEnv
    }).pipe(
      tap({
        next: (res) => {
          if (res?.report) {
            this.applyReadinessReport(res.report);
          }
          // Refresh model config & env after updating key
          this.fetchModelConfigEnv().subscribe();
        },
        error: (err) => {
          console.error(`Failed to update credentials for ${provider}:`, err);
        }
      })
    );
  }
}

export interface ModelConfigEnvResponse {
  config_path: string;
  config_filename: string;
  config_content: string;
  default_model: {
    provider?: string;
    model?: string;
    thinking_level?: string;
    fallback?: {
      provider?: string;
      model?: string;
      thinking_level?: string;
    };
  };
  presets: Record<string, {
    provider: string;
    model: string;
    fallback?: { provider: string; model: string };
  }>;
  env_path: string;
  env_filename: string;
  env_vars: Array<{
    name: string;
    provider: string;
    is_set: boolean;
    preview: string | null;
    description: string;
  }>;
}

