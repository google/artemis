import {
  contextPercent,
  formatCompactTokens,
  formatElapsed,
  parseRunTuning,
  sessionElapsedSeconds,
  tuningLabel
} from './run-info.util';

describe('run-info.util', () => {
  describe('formatElapsed', () => {
    it('formats seconds, minutes and hours with zero padding', () => {
      expect(formatElapsed(0)).toBe('0s');
      expect(formatElapsed(42)).toBe('42s');
      expect(formatElapsed(65)).toBe('1m 05s');
      expect(formatElapsed(3725)).toBe('1h 02m 05s');
    });

    it('treats invalid or negative input as zero', () => {
      expect(formatElapsed(-5)).toBe('0s');
      expect(formatElapsed(undefined)).toBe('0s');
      expect(formatElapsed(Number.NaN)).toBe('0s');
    });
  });

  describe('formatCompactTokens', () => {
    it('uses plain, k and M units', () => {
      expect(formatCompactTokens(842)).toBe('842');
      expect(formatCompactTokens(76_400)).toBe('76.4k');
      expect(formatCompactTokens(1_000)).toBe('1k');
      expect(formatCompactTokens(1_250_000)).toBe('1.25M');
      expect(formatCompactTokens(null)).toBe('0');
    });
  });

  describe('contextPercent', () => {
    it('reports one-decimal share of the window and clamps at 100', () => {
      expect(contextPercent(150_000, 1_000_000)).toBe(15);
      expect(contextPercent(123_456, 1_000_000)).toBe(12.3);
      expect(contextPercent(2_000_000, 1_000_000)).toBe(100);
    });

    it('falls back to the 1M window and is null when usage is unknown', () => {
      expect(contextPercent(250_000, undefined)).toBe(25);
      expect(contextPercent(null, 1_000_000)).toBeNull();
      expect(contextPercent(undefined, 1_000_000)).toBeNull();
    });
  });

  describe('sessionElapsedSeconds', () => {
    it('counts to now while running and to end_time once finished', () => {
      const session = { start_time: 1_000, end_time: 1_090 };
      expect(sessionElapsedSeconds(session, true, 1_200_000)).toBe(200);
      expect(sessionElapsedSeconds(session, false, 1_200_000)).toBe(90);
    });

    it('is zero without a start time or session', () => {
      expect(sessionElapsedSeconds(null, true, 5_000)).toBe(0);
      expect(sessionElapsedSeconds({ start_time: 0 }, true, 5_000)).toBe(0);
    });
  });

  describe('tuningLabel', () => {
    it('maps ids to the launcher labels and defaults unknown ids', () => {
      expect(tuningLabel('verify', 'checkpoints')).toBe('Every step');
      expect(tuningLabel('verify', 'nonsense')).toBe('At the end');
      expect(tuningLabel('explore', 'ultra')).toBe('Close-up');
      expect(tuningLabel('explore', null)).toBe('Quick glance');
    });
  });

  describe('parseRunTuning', () => {
    it('reads run_tuning from a JSON string or an object', () => {
      const asString = JSON.stringify({
        profile: 'pro',
        run_tuning: { verification_level: 'strict', explorer_mode: 'pro' }
      });
      expect(parseRunTuning(asString)).toEqual({ verification_level: 'strict', explorer_mode: 'pro' });
      expect(parseRunTuning({ run_tuning: { verification_level: 'off' } })).toEqual({
        verification_level: 'off',
        explorer_mode: null
      });
    });

    it('is null for flash sessions, malformed JSON and empty tuning', () => {
      expect(parseRunTuning(JSON.stringify({ profile: 'flash' }))).toBeNull();
      expect(parseRunTuning('{not json')).toBeNull();
      expect(parseRunTuning({ run_tuning: {} })).toBeNull();
      expect(parseRunTuning(null)).toBeNull();
    });
  });
});
