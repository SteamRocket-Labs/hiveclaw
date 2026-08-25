import { describe, expect, it } from 'vitest';

import {
  backfillVisibleSessionOnRefocus,
  runDurableHistoryPollTick,
} from './useSessionTransportController';

// UI-005 D1/D2 behavioral regressions. The transport banner promises that the
// durable-history view "recovers automatically"; one failed REST page must
// never kill the polling chain or surface as an unhandled rejection, and a
// failed tab-refocus backfill must not advance the projection cursor while a
// later healthy refocus still recovers.

function trackUnhandledRejections(): { seen: unknown[]; stop: () => void } {
  const seen: unknown[] = [];
  const listener = (reason: unknown) => seen.push(reason);
  process.on('unhandledRejection', listener);
  return { seen, stop: () => process.off('unhandledRejection', listener) };
}

const flushMicrotasks = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

describe('runDurableHistoryPollTick', () => {
  it('reschedules and retries after a rejected backfill without unhandled rejections', async () => {
    const unhandled = trackUnhandledRejections();
    try {
      let calls = 0;
      const scheduled: Array<{ handler: () => void; delay: number }> = [];
      const successes: number[] = [];
      const failures: unknown[] = [];
      const config = {
        isCancelled: () => false,
        isOnline: () => true,
        backfill: async () => {
          calls += 1;
          if (calls === 1) throw new TypeError('Failed to fetch');
          return ['event'];
        },
        onBackfillSucceeded: () => successes.push(calls),
        onBackfillFailed: (error: unknown) => failures.push(error),
        nextDelayMs: () => 3000,
        schedule: (handler: () => void, delay: number) => scheduled.push({ handler, delay }),
      };

      await runDurableHistoryPollTick(config);

      // First tick failed but the chain survived: exactly one failure recorded,
      // success callback skipped, and the next poll is scheduled.
      expect(calls).toBe(1);
      expect(failures).toHaveLength(1);
      expect(String(failures[0])).toContain('Failed to fetch');
      expect(successes).toEqual([]);
      expect(scheduled).toHaveLength(1);
      expect(scheduled[0].delay).toBe(3000);

      // The rescheduled tick runs the backfill again and now succeeds.
      scheduled[0].handler();
      await flushMicrotasks();
      await flushMicrotasks();
      expect(calls).toBe(2);
      expect(successes).toEqual([2]);
      expect(scheduled).toHaveLength(2);

      await flushMicrotasks();
      expect(unhandled.seen).toEqual([]);
    } finally {
      unhandled.stop();
    }
  });

  it('stops the chain when cancelled and ends cleanly when no next interval exists', async () => {
    const scheduled: Array<() => void> = [];
    let cancelled = false;
    let calls = 0;

    await runDurableHistoryPollTick({
      isCancelled: () => cancelled,
      isOnline: () => true,
      backfill: async () => {
        calls += 1;
      },
      onBackfillSucceeded: () => {},
      onBackfillFailed: () => {
        throw new Error('must not fail');
      },
      nextDelayMs: () => (calls >= 1 ? null : 100),
      schedule: (handler: () => void) => scheduled.push(handler),
    });

    // nextDelayMs returned null: no reschedule.
    expect(scheduled).toHaveLength(0);

    cancelled = true;
    await runDurableHistoryPollTick({
      isCancelled: () => cancelled,
      isOnline: () => true,
      backfill: async () => {
        calls += 1;
      },
      onBackfillSucceeded: () => {},
      onBackfillFailed: () => {},
      nextDelayMs: () => 100,
      schedule: (handler: () => void) => scheduled.push(handler),
    });

    // Cancelled before the tick: no backfill, no reschedule.
    expect(calls).toBe(1);
    expect(scheduled).toHaveLength(0);
  });
});

describe('backfillVisibleSessionOnRefocus', () => {
  it('contains a rejected refocus backfill: no cursor advance, no unhandled rejection, later refocus recovers', async () => {
    const unhandled = trackUnhandledRejections();
    try {
      const successes: number[] = [];
      const failures: unknown[] = [];
      let rejectNext = true;

      const refocus = () =>
        backfillVisibleSessionOnRefocus({
          backfill: () =>
            rejectNext ? Promise.reject(new TypeError('Failed to fetch')) : Promise.resolve(['event']),
          onBackfillSucceeded: () => successes.push(1),
          onBackfillFailed: (error: unknown) => failures.push(error),
        });

      refocus();
      await flushMicrotasks();
      // Failure is contained: recorded, cursor not advanced, nothing unhandled.
      expect(failures).toHaveLength(1);
      expect(successes).toEqual([]);
      expect(unhandled.seen).toEqual([]);

      // A later healthy refocus still recovers the projection.
      rejectNext = false;
      refocus();
      await flushMicrotasks();
      expect(successes).toHaveLength(1);
      expect(failures).toHaveLength(1);
      expect(unhandled.seen).toEqual([]);
    } finally {
      unhandled.stop();
    }
  });
});
