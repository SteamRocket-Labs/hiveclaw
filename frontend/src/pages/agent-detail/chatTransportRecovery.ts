export type ChatTransportPhase = 'initializing' | 'connected' | 'reconnecting' | 'degraded' | 'offline' | 'auth_failed';

export const CHAT_TRANSPORT_DEGRADED_AFTER_ATTEMPTS = 5;
export const CHAT_TRANSPORT_MAX_RECONNECT_DELAY_MS = 60_000;
const SESSION_SOCKET_AUTH_CLOSE_CODES = new Set([4002, 4003, 4401, 4403]);

export function isSessionSocketAuthClose(code: number): boolean {
  return SESSION_SOCKET_AUTH_CLOSE_CODES.has(code);
}

export function shouldReconnectSessionSocket(code: number, reconnectDisabled: boolean): boolean {
  return !reconnectDisabled && !isSessionSocketAuthClose(code);
}

export function reconnectDelayMs(attempt: number, randomValue: number = Math.random()): number {
  const safeAttempt = Math.max(0, Math.floor(attempt));
  const cappedBase = Math.min(
    2_000 * (2 ** Math.min(safeAttempt, CHAT_TRANSPORT_DEGRADED_AFTER_ATTEMPTS)),
    CHAT_TRANSPORT_MAX_RECONNECT_DELAY_MS,
  );
  const safeRandom = Math.max(0, Math.min(1, randomValue));
  return Math.round(cappedBase * (0.5 + safeRandom * 0.5));
}

export function isRetryableSessionReadError(error: unknown): boolean {
  if (error instanceof TypeError) return true;
  if (!error || typeof error !== 'object') return false;
  const candidate = error as { name?: unknown; status?: unknown };
  if (candidate.name === 'AbortError') return false;
  const status = typeof candidate.status === 'number' ? candidate.status : Number(candidate.status);
  return status === 408 || status === 425 || status === 429 || status >= 500;
}

type SessionReadRetryState = {
  attempt: number;
  delayMs: number;
};

type SessionReadRetryOptions = {
  signal?: AbortSignal;
  randomValue?: () => number;
  onRetry?: (state: SessionReadRetryState) => void;
  wait?: (delayMs: number, signal?: AbortSignal) => Promise<void>;
};

function sessionReadAbortError(): DOMException {
  return new DOMException('Session read aborted', 'AbortError');
}

function waitForSessionReadRetry(delayMs: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(sessionReadAbortError());
      return;
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, delayMs);
    const onAbort = () => {
      clearTimeout(timer);
      reject(sessionReadAbortError());
    };
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

/**
 * Session history is durable evidence. A temporary network or server outage
 * must keep the current timeline intact and retry until that evidence is
 * readable; switching sessions aborts the loop. Authentication and not-found
 * outcomes remain terminal instead of being hidden behind retries.
 */
export async function retrySessionRead<T>(
  read: () => Promise<T>,
  options: SessionReadRetryOptions = {},
): Promise<T> {
  let attempt = 0;
  const wait = options.wait ?? waitForSessionReadRetry;
  const randomValue = options.randomValue ?? Math.random;
  while (true) {
    if (options.signal?.aborted) throw sessionReadAbortError();
    try {
      return await read();
    } catch (error) {
      if (options.signal?.aborted) throw sessionReadAbortError();
      if (!isRetryableSessionReadError(error)) throw error;
      const delayMs = reconnectDelayMs(attempt, randomValue());
      attempt += 1;
      options.onRetry?.({ attempt, delayMs });
      await wait(delayMs, options.signal);
    }
  }
}

export function chatTransportPhase(input: {
  online: boolean;
  connected: boolean;
  attempts: number;
  everReady: boolean;
  authFailed?: boolean;
}): ChatTransportPhase {
  if (input.authFailed) return 'auth_failed';
  if (!input.online) return 'offline';
  if (input.connected) return 'connected';
  if (input.attempts >= CHAT_TRANSPORT_DEGRADED_AFTER_ATTEMPTS) return 'degraded';
  return input.everReady ? 'reconnecting' : 'initializing';
}

export function transportPollIntervalMs(phase: ChatTransportPhase, hasActiveRun: boolean): number | null {
  if (phase !== 'reconnecting' && phase !== 'degraded') return null;
  return hasActiveRun ? 3_000 : 10_000;
}

type TranscriptIdentity = {
  id?: unknown;
  sequence?: unknown;
};

function transcriptIdentity(event: TranscriptIdentity, fallback: string): string {
  if (typeof event.sequence === 'number' && Number.isFinite(event.sequence) && event.sequence > 0) {
    return `sequence:${event.sequence}`;
  }
  if (typeof event.id === 'string' && event.id.trim()) return `id:${event.id.trim()}`;
  return fallback;
}

export function mergeTranscriptBackfill<T extends TranscriptIdentity>(existing: readonly T[], incoming: readonly T[]): T[] {
  const byIdentity = new Map<string, T>();
  existing.forEach((event, index) => byIdentity.set(transcriptIdentity(event, `existing:${index}`), event));
  incoming.forEach((event, index) => byIdentity.set(transcriptIdentity(event, `incoming:${index}`), event));
  return [...byIdentity.values()].sort((left, right) => {
    const leftSequence = typeof left.sequence === 'number' ? left.sequence : Number.MAX_SAFE_INTEGER;
    const rightSequence = typeof right.sequence === 'number' ? right.sequence : Number.MAX_SAFE_INTEGER;
    return leftSequence - rightSequence;
  });
}

export function latestTranscriptSequence(events: readonly TranscriptIdentity[]): number {
  return events.reduce((latest, event) => (
    typeof event.sequence === 'number' && Number.isFinite(event.sequence)
      ? Math.max(latest, event.sequence)
      : latest
  ), 0);
}
