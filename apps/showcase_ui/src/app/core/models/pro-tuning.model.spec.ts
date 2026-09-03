import {
  DEFAULT_EXPLORER_MODE,
  DEFAULT_VERIFICATION_LEVEL,
  EXPLORER_MODES,
  VERIFICATION_LEVELS,
  levelIndex,
  notchPercent
} from './pro-tuning.model';

describe('pro tuning ladders', () => {
  it('keeps every notch id and label unique within its ladder', () => {
    for (const ladder of [VERIFICATION_LEVELS, EXPLORER_MODES]) {
      expect(new Set(ladder.map((l) => l.id)).size).toBe(ladder.length);
      expect(new Set(ladder.map((l) => l.label)).size).toBe(ladder.length);
    }
  });

  it('mirrors the backend wire values', () => {
    expect(VERIFICATION_LEVELS.map((l) => l.id)).toEqual(['off', 'final', 'checkpoints', 'strict']);
    expect(EXPLORER_MODES.map((l) => l.id)).toEqual(['flash', 'pro', 'ultra']);
    expect(VERIFICATION_LEVELS.map((l) => l.id)).toContain(DEFAULT_VERIFICATION_LEVEL);
    expect(EXPLORER_MODES.map((l) => l.id)).toContain(DEFAULT_EXPLORER_MODE);
  });

  it('every level explains itself', () => {
    for (const level of [...VERIFICATION_LEVELS, ...EXPLORER_MODES]) {
      expect(level.tagline.length).toBeGreaterThan(10);
      expect(level.latency.length).toBeGreaterThan(0);
      expect(level.runs.length).toBeGreaterThan(0);
      expect(level.bestFor.length).toBeGreaterThan(10);
    }
  });

  it('resolves ids case-insensitively and falls back to the default', () => {
    expect(levelIndex(VERIFICATION_LEVELS, ' STRICT ', DEFAULT_VERIFICATION_LEVEL)).toBe(3);
    expect(levelIndex(VERIFICATION_LEVELS, 'nope', DEFAULT_VERIFICATION_LEVEL)).toBe(1);
    expect(levelIndex(EXPLORER_MODES, null, DEFAULT_EXPLORER_MODE)).toBe(0);
  });

  it('maps notches onto the track', () => {
    expect(notchPercent(0, 4)).toBe(0);
    expect(notchPercent(3, 4)).toBe(100);
    expect(notchPercent(1, 3)).toBe(50);
    expect(notchPercent(9, 3)).toBe(100);
    expect(notchPercent(0, 1)).toBe(0);
  });
});
