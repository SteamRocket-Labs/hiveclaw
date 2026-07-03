import React from 'react';

import type { AgentChatMessage } from './chatRuntime';

export type ChatMessagesUpdater = (previous: AgentChatMessage[]) => AgentChatMessage[];
type Listener = () => void;

export interface SessionMessageFrameScheduler {
  requestFrame: (callback: FrameRequestCallback) => number;
  cancelFrame: (id: number) => void;
}

export interface SessionMessageStore {
  getSnapshot: (sessionId: string | null | undefined) => AgentChatMessage[];
  subscribe: (sessionId: string | null | undefined, listener: Listener) => () => void;
  enqueueUpdate: (sessionId: string | null | undefined, update: ChatMessagesUpdater) => void;
  updateAfterQueued: (sessionId: string | null | undefined, update: ChatMessagesUpdater) => void;
  flushSession: (sessionId: string | null | undefined) => void;
  clearSession: (sessionId: string | null | undefined) => void;
  clearAll: () => void;
}

const EMPTY_MESSAGES: AgentChatMessage[] = Object.freeze([]) as unknown as AgentChatMessage[];

function normalizeSessionId(sessionId: string | null | undefined): string {
  return sessionId ? String(sessionId) : '';
}

function createDefaultFrameScheduler(): SessionMessageFrameScheduler {
  return {
    requestFrame: (callback) => {
      if (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function') {
        return window.requestAnimationFrame(callback);
      }
      return setTimeout(() => callback(Date.now()), 16) as unknown as number;
    },
    cancelFrame: (id) => {
      if (typeof window !== 'undefined' && typeof window.cancelAnimationFrame === 'function') {
        window.cancelAnimationFrame(id);
        return;
      }
      clearTimeout(id);
    },
  };
}

export function createSessionMessageStore(
  scheduler: SessionMessageFrameScheduler = createDefaultFrameScheduler(),
): SessionMessageStore {
  const snapshots = new Map<string, AgentChatMessage[]>();
  const listeners = new Map<string, Set<Listener>>();
  const queues = new Map<string, ChatMessagesUpdater[]>();
  const scheduledFrames = new Map<string, number>();

  const getSnapshot = (sessionId: string | null | undefined): AgentChatMessage[] => {
    const key = normalizeSessionId(sessionId);
    if (!key) return EMPTY_MESSAGES;
    return snapshots.get(key) ?? EMPTY_MESSAGES;
  };

  const notify = (key: string) => {
    listeners.get(key)?.forEach((listener) => listener());
  };

  const setSnapshot = (key: string, next: AgentChatMessage[]) => {
    const previous = snapshots.get(key) ?? EMPTY_MESSAGES;
    if (Object.is(previous, next)) return;
    snapshots.set(key, next);
    notify(key);
  };

  const cancelScheduledFrame = (key: string) => {
    const frame = scheduledFrames.get(key);
    if (frame == null) return;
    scheduler.cancelFrame(frame);
    scheduledFrames.delete(key);
  };

  const applyQueuedUpdates = (key: string, base: AgentChatMessage[]): AgentChatMessage[] => {
    const queued = queues.get(key);
    if (!queued || queued.length === 0) return base;
    queues.delete(key);
    return queued.reduce((next, update) => update(next), base);
  };

  const flushSession = (sessionId: string | null | undefined) => {
    const key = normalizeSessionId(sessionId);
    if (!key) return;
    scheduledFrames.delete(key);
    setSnapshot(key, applyQueuedUpdates(key, getSnapshot(key)));
  };

  const scheduleFlush = (key: string) => {
    if (scheduledFrames.has(key)) return;
    const frame = scheduler.requestFrame(() => flushSession(key));
    scheduledFrames.set(key, frame);
  };

  return {
    getSnapshot,
    subscribe: (sessionId, listener) => {
      const key = normalizeSessionId(sessionId);
      if (!key) return () => undefined;
      const bucket = listeners.get(key) ?? new Set<Listener>();
      bucket.add(listener);
      listeners.set(key, bucket);
      return () => {
        bucket.delete(listener);
        if (bucket.size === 0) listeners.delete(key);
      };
    },
    enqueueUpdate: (sessionId, update) => {
      const key = normalizeSessionId(sessionId);
      if (!key) return;
      queues.set(key, [...(queues.get(key) ?? []), update]);
      scheduleFlush(key);
    },
    updateAfterQueued: (sessionId, update) => {
      const key = normalizeSessionId(sessionId);
      if (!key) return;
      cancelScheduledFrame(key);
      const base = applyQueuedUpdates(key, getSnapshot(key));
      setSnapshot(key, update(base));
    },
    flushSession,
    clearSession: (sessionId) => {
      const key = normalizeSessionId(sessionId);
      if (!key) return;
      cancelScheduledFrame(key);
      queues.delete(key);
      const hadSnapshot = snapshots.delete(key);
      if (hadSnapshot) notify(key);
    },
    clearAll: () => {
      const affected = new Set<string>([
        ...snapshots.keys(),
        ...queues.keys(),
        ...scheduledFrames.keys(),
      ]);
      scheduledFrames.forEach((frame) => scheduler.cancelFrame(frame));
      scheduledFrames.clear();
      queues.clear();
      snapshots.clear();
      affected.forEach((key) => notify(key));
    },
  };
}

export function useSessionMessages(
  store: SessionMessageStore,
  sessionId: string | null | undefined,
): AgentChatMessage[] {
  const key = normalizeSessionId(sessionId);
  return React.useSyncExternalStore(
    React.useCallback((listener) => store.subscribe(key, listener), [key, store]),
    React.useCallback(() => store.getSnapshot(key), [key, store]),
    () => EMPTY_MESSAGES,
  );
}
