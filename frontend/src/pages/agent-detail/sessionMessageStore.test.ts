import { describe, expect, it } from 'vitest';

import {
  createSessionMessageStore,
  type SessionMessageFrameScheduler,
} from './sessionMessageStore';
import type { AgentChatMessage } from './chatRuntime';

function message(id: string, content = id): AgentChatMessage {
  return { id, role: 'assistant', content };
}

function createManualScheduler(): SessionMessageFrameScheduler & { runNext: () => void; pendingCount: () => number } {
  const callbacks = new Map<number, FrameRequestCallback>();
  let nextId = 1;
  return {
    requestFrame: (callback) => {
      const id = nextId++;
      callbacks.set(id, callback);
      return id;
    },
    cancelFrame: (id) => {
      callbacks.delete(id);
    },
    runNext: () => {
      const first = callbacks.entries().next().value as [number, FrameRequestCallback] | undefined;
      if (!first) return;
      callbacks.delete(first[0]);
      first[1](performance.now());
    },
    pendingCount: () => callbacks.size,
  };
}

describe('session message store', () => {
  it('coalesces multiple active-session updates into one frame notification', () => {
    const scheduler = createManualScheduler();
    const store = createSessionMessageStore(scheduler);
    const snapshots: AgentChatMessage[][] = [];
    store.subscribe('session-1', () => snapshots.push(store.getSnapshot('session-1')));

    store.enqueueUpdate('session-1', (prev) => [...prev, message('a')]);
    store.enqueueUpdate('session-1', (prev) => [...prev, message('b')]);

    expect(scheduler.pendingCount()).toBe(1);
    expect(snapshots).toHaveLength(0);

    scheduler.runNext();

    expect(snapshots).toHaveLength(1);
    expect(store.getSnapshot('session-1').map((item) => item.id)).toEqual(['a', 'b']);
  });

  it('keeps subscriptions isolated per session', () => {
    const scheduler = createManualScheduler();
    const store = createSessionMessageStore(scheduler);
    const sessionOneSnapshots: AgentChatMessage[][] = [];
    const sessionTwoSnapshots: AgentChatMessage[][] = [];
    store.subscribe('session-1', () => sessionOneSnapshots.push(store.getSnapshot('session-1')));
    store.subscribe('session-2', () => sessionTwoSnapshots.push(store.getSnapshot('session-2')));

    store.enqueueUpdate('session-1', (prev) => [...prev, message('only-session-1')]);
    scheduler.runNext();

    expect(sessionOneSnapshots).toHaveLength(1);
    expect(sessionTwoSnapshots).toHaveLength(0);
  });

  it('applies queued streaming chunks before terminal replacement updates', () => {
    const scheduler = createManualScheduler();
    const store = createSessionMessageStore(scheduler);

    store.enqueueUpdate('session-1', (prev) => [...prev, message('queued')]);
    store.updateAfterQueued('session-1', (prev) => [...prev, message('terminal')]);

    expect(scheduler.pendingCount()).toBe(0);
    expect(store.getSnapshot('session-1').map((item) => item.id)).toEqual(['queued', 'terminal']);
  });
});
