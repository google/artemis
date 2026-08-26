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

import { Component, signal, computed, effect, inject, OnInit, OnDestroy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import { AgentService } from '../../services/agent.service';
import { SystemService } from '../../services/system.service';
import { DeviceInfo, ProbeResult } from '../../core/models/system.model';
import {
  AppReference,
  SmartSuggestion,
  SuggestionCategory
} from '../../core/data/smart-tasks.data';
import { TaskRecommendationService } from '../../core/services/task-recommendation.service';

export type { AppReference, SmartSuggestion, SuggestionCategory };


@Component({
  selector: 'app-home',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './home.component.html',
  styleUrl: './home.component.scss'
})
export class HomeComponent implements OnInit, OnDestroy {
  public agentService = inject(AgentService);
  public systemService = inject(SystemService);
  public taskRecService = inject(TaskRecommendationService);
  private router = inject(Router);

  // High-level navigation mode: 'diagnostics' (System Setup Guide) vs 'launcher' (Task Execution)
  public activeTab = signal<'diagnostics' | 'launcher'>('launcher');

  // Interactive guide sub-tab inside ADB section: 'emulator' | 'usb' | 'wifi'
  public activeAdbGuideTab = signal<'emulator' | 'usb' | 'wifi'>('emulator');
  public emulatorSetupMode = signal<'studio' | 'cli'>('studio');

  // Interactive guide tab for LLM / OCR credentials: 'gemini' | 'ocr'
  public modelSetupMode = signal<'gemini' | 'custom'>('gemini');
  public showOcrConfig = signal<boolean>(false);
  public showFullConfigFile = signal<boolean>(false);

  // Model & Environment configuration from backend
  public modelConfigEnv = computed(() => this.systemService.modelConfigEnv());

  // Google Gemini API Key State
  public geminiKeyInput = signal<string>('');
  public showGeminiKey = signal<boolean>(false);
  public isSavingGeminiKey = signal<boolean>(false);
  public isTestingGeminiKey = signal<boolean>(false);
  public geminiSaveMessage = signal<string | null>(null);
  public geminiSaveError = signal<string | null>(null);
  public isGeminiKeyEdited = signal<boolean>(false);

  // Vision OCR API Key State
  public ocrKeyInput = signal<string>('');
  public showOcrKey = signal<boolean>(false);
  public isSavingOcrKey = signal<boolean>(false);
  public isTestingOcrKey = signal<boolean>(false);
  public ocrSaveMessage = signal<string | null>(null);
  public ocrSaveError = signal<string | null>(null);
  public isOcrKeyEdited = signal<boolean>(false);

  // Clipboard copy state tracker for interactive feedback
  public copiedId = signal<string | null>(null);

  // Diagnostic re-check state
  public isRefreshingDiagnostics = signal<boolean>(false);

  // Wireless ADB Interactive connection signals
  public wifiHost = signal<string>('192.168.1.100');
  public wifiPort = signal<string>('5555');
  public isConnectingWifi = signal<boolean>(false);
  public wifiConnectMessage = signal<string | null>(null);
  public wifiConnectError = signal<string | null>(null);
  public adbRestartFeedback = signal<string | null>(null);

  public wifiCommand = computed(() => {
    const h = this.wifiHost().trim() || '<phone-ip>';
    const p = this.wifiPort().trim() || '5555';
    return `adb connect ${h}:${p}`;
  });

  // Task execution parameters
  public selectedProfile = signal<'flash' | 'pro'>('flash');
  public taskGoal = signal<string>('');
  public isSubmitting = signal<boolean>(false);
  public errorMessage = signal<string | null>(null);

  // Pro Mode Outputter & Structured Output Configuration
  public expectedOutput = signal<string>('');
  public enableOutputter = signal<boolean>(true);
  public showOutputterDrawer = signal<boolean>(false);

  public toggleOutputterDrawer(): void {
    this.showOutputterDrawer.update((v) => !v);
  }

  public applyOutputPreset(preset: string): void {
    if (this.expectedOutput() === preset) {
      this.expectedOutput.set('');
    } else {
      this.expectedOutput.set(preset);
      this.showOutputterDrawer.set(true);
    }
  }

  // Smart intent detection for model recommendation
  public isIntentSuggestingPro = computed<boolean>(() => {
    const text = this.taskGoal().toLowerCase();
    if (!text.trim()) return false;
    const keywords = [
      'monitor', 'polling', 'poll', 'wait until', 'loop', 'keep watching',
      'crash', 'logcat', 'troubleshoot', 'diagnose', 'debug', 'investigate',
      'compare', 'extract', 'summarize', 'report',
      '监控', '轮询', '等待', '一直', '直到', '崩溃', '闪退', '排查', '分析日志', '对比', '总结'
    ];
    return keywords.some(k => text.includes(k));
  });

  public showIntentSuggestion = computed<boolean>(() => {
    return this.isIntentSuggestingPro() && this.selectedProfile() === 'flash';
  });

  // Computed helper states delegating to SystemService
  public isReady = computed(() => this.systemService.isReady());
  public hasReadinessReport = computed(() => this.systemService.hasReadinessReport());
  public isLoading = computed(() => this.systemService.isLoading());
  public isRestartingAdb = computed(() => this.systemService.isRestartingAdb());
  public launchingAvd = computed(() => this.systemService.launchingAvd());
  public emulatorLaunchState = computed(() => this.systemService.emulatorLaunchState());
  public isEmulatorLaunching = computed(() => this.systemService.isEmulatorLaunching());
  public showLaunchLogs = signal<boolean>(false);
  
  // Probes
  public pythonProbe = computed(() => this.systemService.pythonProbe());
  public configProbe = computed(() => this.systemService.configProbe());
  public adbProbe = computed(() => this.systemService.adbProbe());
  public llmProbe = computed(() => this.systemService.llmProbe());
  public geminiProbe = computed(() => this.systemService.geminiProbe());
  public ocrProbe = computed(() => this.systemService.ocrProbe());
  public toolchainProbe = computed(() => this.systemService.toolchainProbe());

  // Step-level readiness
  public isEnvironmentReady = computed(() => this.systemService.isEnvironmentReady());
  public isCredentialsReady = computed(() => this.systemService.isCredentialsReady());
  public isSkipCredentialsCheck = computed(() => this.systemService.isSkipCredentialsCheck());
  public isDeviceReady = computed(() => this.systemService.isDeviceReady());

  // Device information
  public activeDevice = computed(() => this.systemService.activeDevice());
  public connectedDevices = computed(() => this.systemService.connectedDevices());
  public installedAvds = computed(() => this.systemService.installedAvds());
  public emulatorPath = computed(() => this.systemService.emulatorPath());
  public isEmulatorInPath = computed(() => this.systemService.isEmulatorInPath());
  public totalStepCount = computed(() => this.systemService.totalStepCount());
  public passedStepCount = computed(() => this.systemService.passedStepCount());
  public blockerCount = computed(() => this.systemService.blockerCount());
  public passedBlockerCount = computed(() => this.systemService.passedBlockerCount());

  // Configured LLM providers from probe metadata
  public configuredLlmProviders = computed<any[]>(() => {
    const meta = this.llmProbe()?.metadata;
    if (meta && Array.isArray(meta['providers'])) {
      return meta['providers'];
    }
    return [];
  });

  // Multi-OS detection & active OS selection
  public selectedOs = signal<'linux' | 'darwin' | 'windows' | null>(null);
  public effectiveOs = computed<'linux' | 'darwin' | 'windows'>(() => {
    return this.selectedOs() || this.systemService.osType();
  });

  public oneClickSetupCmd = computed(() => {
    const os = this.effectiveOs();
    if (os === 'windows') {
      return 'powershell -ExecutionPolicy Bypass -File scripts/install_deps.ps1';
    }
    return 'bash scripts/install_deps.sh';
  });

  public adbInstallCmd = computed(() => {
    const os = this.effectiveOs();
    if (os === 'windows') {
      return 'winget install Google.PlatformTools';
    }
    if (os === 'darwin') {
      return 'brew install android-platform-tools';
    }
    return 'sudo apt-get install -y adb';
  });

  public toolchainInstallCmd = computed(() => {
    const os = this.effectiveOs();
    if (os === 'windows') {
      return 'winget install Gyan.FFmpeg Genymobile.scrcpy';
    }
    if (os === 'darwin') {
      return 'brew install ffmpeg scrcpy';
    }
    return 'sudo apt-get install -y ffmpeg scrcpy';
  });

  public emuHypervisorTitle = computed(() => {
    const os = this.effectiveOs();
    if (os === 'windows') return 'Enable Windows Hypervisor Platform (WHPX)';
    if (os === 'darwin') return 'Verify macOS Hypervisor / Install Tools';
    return 'Enable KVM Hardware Acceleration (Linux)';
  });

  public emuHypervisorDesc = computed(() => {
    const os = this.effectiveOs();
    if (os === 'windows') return 'Enable Windows Hypervisor Platform in PowerShell (Run as Administrator):';
    if (os === 'darwin') return 'macOS uses native Hypervisor.framework. Install SDK tools via Homebrew (or Studio):';
    return 'Ensure virtualization permissions are granted to your user account:';
  });

  public emuHypervisorCmd = computed(() => {
    const os = this.effectiveOs();
    if (os === 'windows') return 'Enable-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform';
    if (os === 'darwin') return 'brew install --cask android-commandlinetools';
    return 'sudo apt-get install -y qemu-kvm libvirt-daemon-system && sudo adduser $USER kvm';
  });

  public emuSdkInstallCmd = computed(() => {
    const os = this.effectiveOs();
    if (os === 'darwin') {
      return 'sdkmanager --install "system-images;android-34;google_apis;arm64-v8a" "emulator" "platform-tools"';
    }
    return 'sdkmanager --install "system-images;android-34;google_apis;x86_64" "emulator" "platform-tools"';
  });

  public emuCreateAvdCmd = computed(() => {
    const os = this.effectiveOs();
    if (os === 'darwin') {
      return 'avdmanager create avd -n Pixel_8_API_34 -k "system-images;android-34;google_apis;arm64-v8a" --device "pixel_8"';
    }
    return 'avdmanager create avd -n Pixel_8_API_34 -k "system-images;android-34;google_apis;x86_64" --device "pixel_8"';
  });

  // Flag indicating whether Google Cloud Vision OCR is configured
  public isOcrConfigured = computed<boolean>(() => {
    const meta = this.ocrProbe()?.metadata;
    return meta?.['configured'] === true;
  });

  public currentApiKey = computed<string>(() => this.systemService.currentApiKey());
  public apiKeysMap = computed<Record<string, string>>(() => this.systemService.apiKeysMap());

  public savedGeminiKey = computed<string>(() => {
    const keys = this.apiKeysMap();
    return keys['google'] || this.currentApiKey() || '';
  });

  public isGeminiModified = computed<boolean>(() => {
    return this.geminiKeyInput().trim() !== this.savedGeminiKey().trim();
  });

  public savedOcrKey = computed<string>(() => {
    const keys = this.apiKeysMap();
    return keys['ocr'] || '';
  });

  public isOcrModified = computed<boolean>(() => {
    return this.ocrKeyInput().trim() !== this.savedOcrKey().trim();
  });

  // Rich Smart Suggestions Library (Device-Aware, Flash vs Pro Tailored)
  public readonly allSuggestions = this.taskRecService.allTasks;

  // Suggestion category filter & shuffle state
  public selectedCategory = signal<SuggestionCategory>('all');
  public shuffleOffset = signal<number>(0);

  // Set of installed package strings from active device
  public installedPackages = computed<Set<string>>(() => {
    const pkgs = this.activeDevice()?.installed_packages;
    if (pkgs && Array.isArray(pkgs)) {
      return new Set(pkgs);
    }
    return new Set();
  });

  public filteredSuggestions = computed<SmartSuggestion[]>(() => {
    return this.taskRecService.filterAndRankTasks(
      this.installedPackages(),
      this.selectedCategory(),
      this.shuffleOffset()
    );
  });




  private focusListener = () => {
    // Silently re-check environment when user returns to the browser tab
    this.systemService.fetchReadiness().subscribe();
  };

  constructor() {
    effect(() => {
      const keys = this.systemService.apiKeysMap();
      const current = this.systemService.currentApiKey();
      const googleKey = keys['google'] || current || '';
      const ocrKey = keys['ocr'] || '';
      if (!this.isGeminiKeyEdited()) {
        this.geminiKeyInput.set(googleKey);
      }
      if (!this.isOcrKeyEdited()) {
        this.ocrKeyInput.set(ocrKey);
      }
    });
  }

  ngOnInit(): void {
    // Initial fetch of system readiness & model configuration
    this.systemService.fetchReadiness().subscribe();
    this.systemService.fetchModelConfigEnv().subscribe();

    window.addEventListener('focus', this.focusListener);
  }

  ngOnDestroy(): void {
    window.removeEventListener('focus', this.focusListener);
  }

  public setTab(tab: 'diagnostics' | 'launcher'): void {
    this.activeTab.set(tab);
    if (tab === 'diagnostics') {
      this.systemService.fetchReadiness().subscribe();
      this.systemService.fetchModelConfigEnv().subscribe();
    }
  }

  public setSelectedOs(os: 'linux' | 'darwin' | 'windows'): void {
    this.selectedOs.set(os);
  }

  public setAdbGuideTab(tab: 'emulator' | 'usb' | 'wifi'): void {
    this.activeAdbGuideTab.set(tab);
  }

  public setEmulatorSetupMode(mode: 'studio' | 'cli'): void {
    this.emulatorSetupMode.set(mode);
  }

  public setModelSetupMode(mode: 'gemini' | 'custom'): void {
    this.modelSetupMode.set(mode);
    if (mode === 'custom') {
      this.systemService.setSkipCredentialsCheck(true);
      this.systemService.fetchModelConfigEnv().subscribe();
    } else {
      this.systemService.setSkipCredentialsCheck(false);
    }
  }

  public toggleFullConfigFile(): void {
    this.showFullConfigFile.update(v => !v);
  }

  public toggleGeminiKeyVisibility(): void {
    this.showGeminiKey.update(v => !v);
  }

  public toggleOcrKeyVisibility(): void {
    this.showOcrKey.update(v => !v);
  }

  public toggleOcrConfig(): void {
    this.showOcrConfig.update(v => !v);
  }

  public onGeminiKeyChange(val: string): void {
    this.geminiKeyInput.set(val);
    this.isGeminiKeyEdited.set(true);
    this.geminiSaveError.set(null);
    this.geminiSaveMessage.set(null);
  }

  public onOcrKeyChange(val: string): void {
    this.ocrKeyInput.set(val);
    this.isOcrKeyEdited.set(true);
    this.ocrSaveError.set(null);
    this.ocrSaveMessage.set(null);
  }

  public saveGeminiKey(): void {
    const key = this.geminiKeyInput().trim();
    if (!key) return;
    this.isSavingGeminiKey.set(true);
    this.geminiSaveError.set(null);
    this.geminiSaveMessage.set(null);

    this.systemService.updateApiKey('google', key, true).subscribe({
      next: (res) => {
        this.isSavingGeminiKey.set(false);
        this.isGeminiKeyEdited.set(false);
        this.geminiSaveMessage.set(res?.message || '✓ Gemini API key verified & saved successfully.');
        setTimeout(() => this.geminiSaveMessage.set(null), 5000);
      },
      error: (err) => {
        this.isSavingGeminiKey.set(false);
        this.geminiSaveError.set(err?.error?.detail || err?.message || 'Failed to update Gemini API key.');
      }
    });
  }

  public clearGeminiKey(): void {
    this.geminiKeyInput.set('');
    this.isGeminiKeyEdited.set(false);
    this.geminiSaveError.set(null);
    this.geminiSaveMessage.set(null);

    if (this.savedGeminiKey().trim()) {
      this.isSavingGeminiKey.set(true);
      this.systemService.updateApiKey('google', '', true).subscribe({
        next: (res) => {
          this.isSavingGeminiKey.set(false);
          this.geminiSaveMessage.set(res?.message || '✓ Gemini API key cleared.');
          setTimeout(() => this.geminiSaveMessage.set(null), 5000);
        },
        error: (err) => {
          this.isSavingGeminiKey.set(false);
          this.geminiSaveError.set(err?.error?.detail || err?.message || 'Failed to clear Gemini API key.');
        }
      });
    } else {
      this.geminiSaveMessage.set('✓ Gemini API key cleared.');
      setTimeout(() => this.geminiSaveMessage.set(null), 3000);
    }
  }

  public saveOcrKey(): void {
    const key = this.ocrKeyInput().trim();
    if (!key) return;
    this.isSavingOcrKey.set(true);
    this.ocrSaveError.set(null);
    this.ocrSaveMessage.set(null);

    this.systemService.updateApiKey('ocr', key, true).subscribe({
      next: (res) => {
        this.isSavingOcrKey.set(false);
        this.isOcrKeyEdited.set(false);
        this.ocrSaveMessage.set(res?.message || '✓ Vision OCR API key verified & saved.');
        setTimeout(() => this.ocrSaveMessage.set(null), 5000);
      },
      error: (err) => {
        this.isSavingOcrKey.set(false);
        this.ocrSaveError.set(err?.error?.detail || err?.message || 'Failed to update Vision OCR key.');
      }
    });
  }

  public clearOcrKey(): void {
    this.ocrKeyInput.set('');
    this.isOcrKeyEdited.set(false);
    this.ocrSaveError.set(null);
    this.ocrSaveMessage.set(null);

    if (this.savedOcrKey().trim()) {
      this.isSavingOcrKey.set(true);
      this.systemService.updateApiKey('ocr', '', true).subscribe({
        next: (res) => {
          this.isSavingOcrKey.set(false);
          this.ocrSaveMessage.set(res?.message || '✓ Vision OCR API key cleared.');
          setTimeout(() => this.ocrSaveMessage.set(null), 5000);
        },
        error: (err) => {
          this.isSavingOcrKey.set(false);
          this.ocrSaveError.set(err?.error?.detail || err?.message || 'Failed to clear Vision OCR key.');
        }
      });
    } else {
      this.ocrSaveMessage.set('✓ Vision OCR API key cleared.');
      setTimeout(() => this.ocrSaveMessage.set(null), 3000);
    }
  }

  public testGeminiKey(): void {
    const key = this.geminiKeyInput().trim();
    if (!key) return;
    this.isTestingGeminiKey.set(true);
    this.geminiSaveError.set(null);
    this.geminiSaveMessage.set(null);

    this.systemService.testApiKey('google', key).subscribe({
      next: (res) => {
        this.isTestingGeminiKey.set(false);
        if (res?.valid) {
          this.geminiSaveMessage.set(res?.message || '✓ Gemini API key is valid!');
        } else {
          this.geminiSaveError.set(res?.message || 'Gemini API key verification failed.');
        }
        setTimeout(() => this.geminiSaveMessage.set(null), 5000);
      },
      error: (err) => {
        this.isTestingGeminiKey.set(false);
        this.geminiSaveError.set(err?.error?.detail || err?.message || 'Gemini API key test failed.');
      }
    });
  }

  public testOcrKey(): void {
    const key = this.ocrKeyInput().trim();
    if (!key) return;
    this.isTestingOcrKey.set(true);
    this.ocrSaveError.set(null);
    this.ocrSaveMessage.set(null);

    this.systemService.testApiKey('ocr', key).subscribe({
      next: (res) => {
        this.isTestingOcrKey.set(false);
        if (res?.valid) {
          this.ocrSaveMessage.set(res?.message || '✓ Vision OCR API key is valid!');
        } else {
          this.ocrSaveError.set(res?.message || 'Vision OCR API key verification failed.');
        }
        setTimeout(() => this.ocrSaveMessage.set(null), 5000);
      },
      error: (err) => {
        this.isTestingOcrKey.set(false);
        this.ocrSaveError.set(err?.error?.detail || err?.message || 'Vision OCR API key test failed.');
      }
    });
  }

  public getProviderDisplayName(tab: string): string {
    switch (tab) {
      case 'gemini': return 'Gemini';
      case 'ocr': return 'Vision OCR';
      default: return tab;
    }
  }

  public getProviderEnvVar(tab: string): string {
    switch (tab) {
      case 'gemini': return 'GEMINI_API_KEY';
      case 'ocr': return 'GOOGLE_VISION_API_KEY';
      default: return 'API_KEY';
    }
  }

  public getProviderHint(tab: string): string {
    switch (tab) {
      case 'gemini':
        return 'For a quick start, Google Gemini provides a free API key. Artemis also supports other models (OpenAI, Claude, OpenRouter, etc.)—you can configure your own API keys directly in .env or your environment.';
      case 'ocr':
        return 'Google Cloud Vision API key for on-screen OCR text detection and UI grounding.';
      default:
        return 'Configure your API key or use environment definitions.';
    }
  }

  public skipCredentialsCheck(): void {
    this.systemService.skipCredentialsCheck();
  }

  public getApiKeyPlaceholder(tab: string): string {
    switch (tab) {
      case 'gemini': return 'Enter Gemini API Key (e.g. AIzaSy...)';
      case 'ocr': return 'Enter Vision OCR API Key (e.g. AIzaSy...)';
      default: return 'Enter API Key...';
    }
  }

  public isTabProviderActive(tab: string): boolean {
    if (tab === 'ocr') {
      return this.isOcrConfigured();
    }
    const targetProvider = tab === 'gemini' ? 'google' : tab;
    const providers = this.configuredLlmProviders();
    return providers.some(p => p.provider === targetProvider);
  }

  public refreshReadiness(): void {
    this.isRefreshingDiagnostics.set(true);
    this.systemService.fetchReadiness(false, true).subscribe({
      next: () => {
        this.systemService.fetchModelConfigEnv().subscribe({
          next: () => {
            setTimeout(() => this.isRefreshingDiagnostics.set(false), 450);
          },
          error: () => this.isRefreshingDiagnostics.set(false)
        });
      },
      error: () => this.isRefreshingDiagnostics.set(false)
    });
  }

  public restartAdbServer(): void {
    this.adbRestartFeedback.set(null);
    this.systemService.restartAdb().subscribe({
      next: (res) => {
        this.adbRestartFeedback.set('ADB Refreshed ✓');
        setTimeout(() => this.adbRestartFeedback.set(null), 2500);
      },
      error: () => {
        this.adbRestartFeedback.set('Restart Failed');
        setTimeout(() => this.adbRestartFeedback.set(null), 3000);
      }
    });
  }

  public connectWifiDevice(): void {
    const host = this.wifiHost().trim();
    const portStr = this.wifiPort().trim() || '5555';
    const port = parseInt(portStr, 10) || 5555;

    if (!host) {
      this.wifiConnectError.set('Please enter a valid IP address.');
      return;
    }

    this.isConnectingWifi.set(true);
    this.wifiConnectError.set(null);
    this.wifiConnectMessage.set(null);

    this.systemService.connectWirelessAdb(host, port).subscribe({
      next: (res) => {
        this.isConnectingWifi.set(false);
        const cr = res?.connect_result;
        if (cr?.success) {
          this.wifiConnectMessage.set(`Connected to ${host}:${port}!`);
          setTimeout(() => this.wifiConnectMessage.set(null), 4000);
        } else {
          this.wifiConnectError.set(cr?.message || 'Connection failed. Please check phone IP & Wi-Fi.');
        }
      },
      error: (err) => {
        this.isConnectingWifi.set(false);
        this.wifiConnectError.set(err?.error?.detail || 'Failed to connect. Please check adb connection.');
      }
    });
  }

  public launchAvdEmulator(avdName: string): void {
    this.systemService.launchEmulator(avdName).subscribe();
  }

  public toggleLaunchLogs(): void {
    this.showLaunchLogs.update(v => !v);
  }

  public stopEmulator(): void {
    this.systemService.stopEmulator().subscribe();
  }

  public dismissEmulatorStatus(): void {
    this.systemService.dismissEmulatorStatus().subscribe();
  }

  public selectTargetDevice(serial: string): void {
    this.systemService.selectDevice(serial).subscribe();
  }

  public getEmulatorCommand(avdName: string): string {
    const p = this.emulatorPath();
    const cmd = this.isEmulatorInPath() ? 'emulator' : (p || '~/Android/Sdk/emulator/emulator');
    return `${cmd} -avd ${avdName}`;
  }

  public copyToClipboard(text: string, id: string): void {
    if (navigator.clipboard) {
      navigator.clipboard.writeText(text).then(() => {
        this.copiedId.set(id);
        setTimeout(() => {
          if (this.copiedId() === id) {
            this.copiedId.set(null);
          }
        }, 2000);
      });
    }
  }

  public setProfile(profile: 'flash' | 'pro'): void {
    this.selectedProfile.set(profile);
  }

  public setCategory(cat: SuggestionCategory): void {
    this.selectedCategory.set(cat);
  }

  public shuffleSuggestions(): void {
    this.shuffleOffset.update(v => v + 3);
  }

  public getAppNamesDisplay(apps: AppReference[]): string {
    return apps.map(a => a.name).join(' + ');
  }

  public applySuggestion(item: SmartSuggestion): void {
    this.taskGoal.set(item.goal);
    this.selectedProfile.set(item.profile);
    this.errorMessage.set(null);
  }

  public applyQuickPrompt(promptGoal: string, profile?: 'flash' | 'pro'): void {
    this.taskGoal.set(promptGoal);
    if (profile) {
      this.selectedProfile.set(profile);
    }
    this.errorMessage.set(null);
  }


  public proceedToLauncher(): void {
    this.activeTab.set('launcher');
  }

  public runTask(): void {
    const goal = this.taskGoal().trim();
    if (!goal) {
      this.errorMessage.set('Please enter a task goal before running.');
      return;
    }

    if (!this.isReady()) {
      this.errorMessage.set('System prerequisites are not satisfied. Please review System Setup first.');
      this.activeTab.set('diagnostics');
      return;
    }

    this.isSubmitting.set(true);
    this.errorMessage.set(null);

    this.agentService
      .runTask(
        goal,
        this.selectedProfile(),
        this.selectedProfile() === 'pro' && this.expectedOutput().trim()
          ? this.expectedOutput().trim()
          : undefined,
        this.selectedProfile() === 'pro' ? this.enableOutputter() : undefined
      )
      .subscribe({
        next: () => {
          this.isSubmitting.set(false);
          this.router.navigate(['/workspace']);
        },
        error: (err) => {
          console.error('Failed to submit task from home page:', err);
          this.isSubmitting.set(false);
          this.errorMessage.set(
            err?.error?.detail || 'Failed to submit task. Please check server connection.'
          );
        }
      });
  }
}
