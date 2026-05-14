/**
 * Tier 3-4 React hook that drives the Deep Research SSE stream and exposes
 * incremental state to consumers. Keeps the hook thin — all parsing /
 * authentication logic lives in `streamDeepResearch`; the hook only owns
 * lifecycle (abort on unmount, cursor checkpointing, event aggregation).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  streamDeepResearch,
  type DeepResearchStreamEvent,
  type DeepResearchStreamEventType,
  type StreamDeepResearchOptions,
} from '../api/domains/deepResearch';

type StreamStatus = 'idle' | 'streaming' | 'completed' | 'failed' | 'aborted';

export interface DeepResearchStreamState {
  status: StreamStatus;
  events: DeepResearchStreamEvent[];
  steps: DeepResearchStreamEvent[];
  claims: DeepResearchStreamEvent[];
  sourceNotes: DeepResearchStreamEvent[];
  laneSummaries: DeepResearchStreamEvent[];
  reflections: DeepResearchStreamEvent[];
  controllerTrace: DeepResearchStreamEvent[];
  reportMarkdown: string;
  finalPayload: Record<string, unknown> | null;
  error: Error | null;
  heartbeats: number;
}

export interface UseDeepResearchStreamOptions extends StreamDeepResearchOptions {
  /** If true, open the connection automatically on mount. Default: true */
  autoStart?: boolean;
}

const _bucketKey: Record<DeepResearchStreamEventType, keyof DeepResearchStreamState | null> = {
  step: 'steps',
  claim: 'claims',
  source_note: 'sourceNotes',
  lane_summary: 'laneSummaries',
  reflection: 'reflections',
  controller_trace: 'controllerTrace',
  report: null,
  final: null,
  heartbeat: null,
  error: null,
};

export const _INITIAL_DEEP_RESEARCH_STREAM_STATE: DeepResearchStreamState = {
  status: 'idle',
  events: [],
  steps: [],
  claims: [],
  sourceNotes: [],
  laneSummaries: [],
  reflections: [],
  controllerTrace: [],
  reportMarkdown: '',
  finalPayload: null,
  error: null,
  heartbeats: 0,
};

export function useDeepResearchStream(
  agentId: string | null | undefined,
  taskId: string | null | undefined,
  options: UseDeepResearchStreamOptions = {},
): DeepResearchStreamState & {
  start: () => void;
  abort: () => void;
} {
  const { autoStart = true, ...streamOptions } = options;
  const [state, setState] = useState<DeepResearchStreamState>(_INITIAL_DEEP_RESEARCH_STREAM_STATE);
  const controllerRef = useRef<AbortController | null>(null);
  const runIdRef = useRef(0);

  const abort = useCallback(() => {
    if (controllerRef.current) {
      controllerRef.current.abort();
      controllerRef.current = null;
    }
  }, []);

  const start = useCallback(() => {
    if (!agentId || !taskId) {
      return;
    }
    abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    runIdRef.current += 1;
    const myRunId = runIdRef.current;

    setState({ ..._INITIAL_DEEP_RESEARCH_STREAM_STATE, status: 'streaming' });

    const run = async () => {
      try {
        for await (const event of streamDeepResearch(agentId, taskId, {
          ...streamOptions,
          signal: controller.signal,
        })) {
          if (runIdRef.current !== myRunId) {
            return; // Stale run; another start() superseded us
          }
          setState((prev) => applyDeepResearchEvent(prev, event));
        }
        if (runIdRef.current !== myRunId) return;
        setState((prev) =>
          prev.status === 'streaming' && prev.finalPayload === null
            ? { ...prev, status: 'completed' }
            : prev,
        );
      } catch (err) {
        if (runIdRef.current !== myRunId) return;
        if ((err as Error)?.name === 'AbortError') {
          setState((prev) => ({ ...prev, status: 'aborted' }));
          return;
        }
        setState((prev) => ({ ...prev, status: 'failed', error: err as Error }));
      } finally {
        if (controllerRef.current === controller) {
          controllerRef.current = null;
        }
      }
    };

    run();
  }, [agentId, taskId, streamOptions, abort]);

  useEffect(() => {
    if (autoStart && agentId && taskId) {
      start();
    }
    return () => {
      abort();
    };
    // We deliberately key on the identifiers, not on streamOptions, so cursor
    // tweaks don't cause spurious reconnects. Callers that want a reconnect
    // call start() explicitly.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId, taskId, autoStart]);

  return useMemo(() => ({ ...state, start, abort }), [state, start, abort]);
}

/**
 * Pure reducer: given the current stream state and the next SSE event, return
 * the new state. Exposed so tests can verify behavior without rendering the
 * React hook.
 */
export function applyDeepResearchEvent(
  prev: DeepResearchStreamState,
  event: DeepResearchStreamEvent,
): DeepResearchStreamState {
  const next: DeepResearchStreamState = {
    ...prev,
    events: [...prev.events, event],
  };

  const bucket = _bucketKey[event.event];
  if (bucket) {
    const list = next[bucket] as DeepResearchStreamEvent[];
    next[bucket] = [...list, event] as never;
  }

  if (event.event === 'report') {
    const markdown = (event.payload?.markdown as string | undefined) ?? '';
    next.reportMarkdown = markdown;
  } else if (event.event === 'final') {
    next.finalPayload = event.payload;
    const status = (event.payload?.status as string | undefined) ?? '';
    next.status = status === 'completed' ? 'completed' : 'failed';
  } else if (event.event === 'heartbeat') {
    next.heartbeats = prev.heartbeats + 1;
  } else if (event.event === 'error') {
    next.status = 'failed';
    const message = (event.payload?.message as string | undefined) ?? 'Stream error';
    next.error = new Error(message);
  }

  return next;
}
