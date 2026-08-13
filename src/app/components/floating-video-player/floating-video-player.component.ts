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

import {
  Component,
  inject,
  signal,
  computed,
  ElementRef,
  ViewChild,
  HostListener,
  effect
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { AgentService } from '../../services/agent.service';

@Component({
  selector: 'app-floating-video-player',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './floating-video-player.component.html',
  styleUrl: './floating-video-player.component.scss'
})
export class FloatingVideoPlayerComponent {
  public agentService = inject(AgentService);

  @ViewChild('videoRef') videoRef?: ElementRef<HTMLVideoElement>;

  // Playback state signals
  public isPlaying = signal<boolean>(false);
  public playbackRate = signal<number>(1.0);
  public isLooping = signal<boolean>(false);
  public currentTime = signal<number>(0);
  public duration = signal<number>(0);
  public isMuted = signal<boolean>(false);
  public isTheaterMode = signal<boolean>(false);
  public videoLoadError = signal<boolean>(false);
  public liveStreamUrl = signal<string>('/api/stream/device-live');
  public liveStreamError = signal<boolean>(false);

  // Available speed options
  public speedOptions = [0.5, 1.0, 1.5, 2.0, 4.0];

  // Dragging and window positioning state
  public posX = signal<number>(
    typeof window !== 'undefined' ? Math.max(20, window.innerWidth - 380) : 100
  );
  public posY = signal<number>(
    typeof window !== 'undefined' ? Math.max(60, window.innerHeight - 660) : 100
  );

  private isDragging = false;
  private dragStartX = 0;
  private dragStartY = 0;
  private initialPosX = 0;
  private initialPosY = 0;

  constructor() {
    // Reset video load error whenever active video URL changes
    effect(
      () => {
        const url = this.agentService.activeVideoUrl();
        this.videoLoadError.set(false);
        this.currentTime.set(0);
        this.duration.set(0);
        this.isPlaying.set(false);
      },
      { allowSignalWrites: true }
    );
  }

  public onLiveStreamError(): void {
    this.liveStreamError.set(true);
  }

  public retryLiveStream(): void {
    this.liveStreamError.set(false);
    this.liveStreamUrl.set(`/api/stream/device-live?t=${Date.now()}`);
  }

  /**
   * Format seconds into mm:ss format
   */
  public formatTime(seconds: number): string {
    if (isNaN(seconds) || seconds < 0) return '00:00';
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    const mStr = mins < 10 ? '0' + mins : String(mins);
    const sStr = secs < 10 ? '0' + secs : String(secs);
    return `${mStr}:${sStr}`;
  }

  /**
   * Progress percentage for timeline scrubber (0 - 100)
   */
  public progressPercent = computed(() => {
    const dur = this.duration();
    if (dur <= 0) return 0;
    return Math.min(100, Math.max(0, (this.currentTime() / dur) * 100));
  });

  /**
   * Start dragging the window from header
   */
  public startDrag(event: MouseEvent): void {
    if (this.isTheaterMode()) return;
    this.isDragging = true;
    this.dragStartX = event.clientX;
    this.dragStartY = event.clientY;
    this.initialPosX = this.posX();
    this.initialPosY = this.posY();
    event.preventDefault();
  }

  @HostListener('window:mousemove', ['$event'])
  public onDrag(event: MouseEvent): void {
    if (!this.isDragging) return;
    const deltaX = event.clientX - this.dragStartX;
    const deltaY = event.clientY - this.dragStartY;

    const minX = 10;
    const minY = 10;
    const maxX = Math.max(10, window.innerWidth - 240);
    const maxY = Math.max(10, window.innerHeight - 80);

    const targetX = Math.max(minX, Math.min(maxX, this.initialPosX + deltaX));
    const targetY = Math.max(minY, Math.min(maxY, this.initialPosY + deltaY));

    this.posX.set(targetX);
    this.posY.set(targetY);
  }

  @HostListener('window:mouseup')
  public stopDrag(): void {
    this.isDragging = false;
  }

  @HostListener('window:resize')
  public onResize(): void {
    // Keep window within viewport bounds if window resized
    const maxX = Math.max(10, window.innerWidth - 240);
    const maxY = Math.max(10, window.innerHeight - 80);
    if (this.posX() > maxX) this.posX.set(maxX);
    if (this.posY() > maxY) this.posY.set(maxY);
  }

  @HostListener('window:keydown', ['$event'])
  public onKeyDown(event: KeyboardEvent): void {
    if (!this.agentService.isVideoWindowOpen()) return;

    if (event.key === 'Escape') {
      if (this.isTheaterMode()) {
        this.isTheaterMode.set(false);
      } else {
        this.agentService.closeVideoPlayer();
      }
    }
  }

  /**
   * Video element event handlers
   */
  public onLoadedMetadata(): void {
    const v = this.videoRef?.nativeElement;
    if (v) {
      this.duration.set(v.duration || 0);
      this.videoLoadError.set(false);
      v.playbackRate = this.playbackRate();
      v.loop = this.isLooping();
    }
  }

  public onTimeUpdate(): void {
    const v = this.videoRef?.nativeElement;
    if (v) {
      this.currentTime.set(v.currentTime);
      if (v.duration && v.duration !== this.duration()) {
        this.duration.set(v.duration);
      }
    }
  }

  public onPlay(): void {
    this.isPlaying.set(true);
  }

  public onPause(): void {
    this.isPlaying.set(false);
  }

  public onEnded(): void {
    this.isPlaying.set(false);
  }

  public onError(): void {
    this.videoLoadError.set(true);
    this.isPlaying.set(false);
  }

  /**
   * Playback actions
   */
  public togglePlay(): void {
    const v = this.videoRef?.nativeElement;
    if (!v) return;
    if (v.paused) {
      v.play().catch(() => {});
    } else {
      v.pause();
    }
  }

  public restart(): void {
    const v = this.videoRef?.nativeElement;
    if (!v) return;
    v.currentTime = 0;
    v.play().catch(() => {});
  }

  public seek(seconds: number): void {
    const v = this.videoRef?.nativeElement;
    if (!v) return;
    v.currentTime = Math.max(0, Math.min(this.duration(), seconds));
  }

  public onScrub(event: Event): void {
    const input = event.target as HTMLInputElement;
    const targetTime = parseFloat(input.value);
    this.seek(targetTime);
  }

  public setSpeed(rate: number): void {
    this.playbackRate.set(rate);
    const v = this.videoRef?.nativeElement;
    if (v) {
      v.playbackRate = rate;
    }
  }

  public toggleLoop(): void {
    const next = !this.isLooping();
    this.isLooping.set(next);
    const v = this.videoRef?.nativeElement;
    if (v) {
      v.loop = next;
    }
  }

  public toggleMute(): void {
    const next = !this.isMuted();
    this.isMuted.set(next);
    const v = this.videoRef?.nativeElement;
    if (v) {
      v.muted = next;
    }
  }

  public toggleTheater(): void {
    this.isTheaterMode.set(!this.isTheaterMode());
  }

  public toggleMinimize(): void {
    this.agentService.isVideoMinimized.set(!this.agentService.isVideoMinimized());
  }

  public openInNewTab(): void {
    const url = this.agentService.activeVideoUrl();
    if (url) {
      window.open(url, '_blank');
    }
  }
}
