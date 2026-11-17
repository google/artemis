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

import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { of } from 'rxjs';

import { AgentService } from '../../services/agent.service';
import { SystemService } from '../../services/system.service';
import { TaskRecommendationService } from '../../core/services/task-recommendation.service';
import { HomeComponent } from './home.component';

describe('HomeComponent model setup mode', () => {
  const storageKey = 'artemis_model_setup_mode';
  let systemService: {
    apiKeysMap: ReturnType<typeof signal<Record<string, string>>>;
    currentApiKey: ReturnType<typeof signal<string>>;
    setSkipCredentialsCheck: jasmine.Spy;
    fetchReadiness: jasmine.Spy;
    fetchModelConfigEnv: jasmine.Spy;
    fetchAdbServerStatus: jasmine.Spy;
  };

  beforeEach(async () => {
    localStorage.removeItem(storageKey);
    systemService = {
      apiKeysMap: signal<Record<string, string>>({}),
      currentApiKey: signal(''),
      setSkipCredentialsCheck: jasmine.createSpy('setSkipCredentialsCheck'),
      fetchReadiness: jasmine.createSpy('fetchReadiness').and.returnValue(of(null)),
      fetchModelConfigEnv: jasmine.createSpy('fetchModelConfigEnv').and.returnValue(of(null)),
      fetchAdbServerStatus: jasmine.createSpy('fetchAdbServerStatus').and.returnValue(
        of({ endpoint: { mode: 'local' } })
      )
    };

    await TestBed.configureTestingModule({
      imports: [HomeComponent],
      providers: [
        provideRouter([]),
        { provide: AgentService, useValue: { getProTuningDefaults: () => of({}) } },
        { provide: SystemService, useValue: systemService },
        { provide: TaskRecommendationService, useValue: {} }
      ]
    }).compileComponents();
  });

  afterEach(() => localStorage.removeItem(storageKey));

  it('persists and restores the custom setup choice', () => {
    const first = TestBed.createComponent(HomeComponent);
    first.componentInstance.setModelSetupMode('custom');

    expect(localStorage.getItem(storageKey)).toBe('custom');
    first.destroy();

    const second = TestBed.createComponent(HomeComponent);
    second.componentInstance.ngOnInit();

    expect(second.componentInstance.modelSetupMode()).toBe('custom');
    expect(systemService.setSkipCredentialsCheck).toHaveBeenCalledWith(true);
    second.destroy();
  });
});
