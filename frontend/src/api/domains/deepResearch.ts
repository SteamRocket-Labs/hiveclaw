/**
 * Tier 3-4 frontend adapter for the Deep Research SSE stream.
 *
 * Wraps fetch() + a ReadableStream reader to parse `data: {...}\n\n` frames
 * out of the `/deep-research/stream/:agent_id/:task_id` endpoint and yield
 * typed events. EventSource is intentionally avoided because the backend
 * authenticates via `Authorization: Bearer …` headers which EventSource
 * cannot send.
 */

import { ApiError } from '../core';

const API_BASE = '/api';

export type DeepResearchStreamEventType =
  | 'step'
  | 'claim'
  | 'source_note'
  | 'lane_summary'
  | 'reflection'
  | 'controller_trace'
  | 'report'
  | 'final'
  | 'heartbeat'
  | 'error';

export interface DeepResearchStreamEvent {
  event: DeepResearchStreamEventType;
  task_id: string;
  timestamp?: string;
  payload: Record<string, unknown>;
}

export interface StreamDeepResearchOptions {
  pollInterval?: number;        // seconds between server poll ticks
  deadlineSeconds?: number;     // server-side overall budget
  afterStep?: number;
  afterClaim?: number;
  afterSourceNote?: number;
  afterLaneSummary?: number;
  afterReflection?: number;
  afterControllerTrace?: number;
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;     // tests inject a stub here
}

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = { Accept: 'text/event-stream' };
  const token = localStorage.getItem('token');
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const tenantId = localStorage.getItem('current_tenant_id');
  if (tenantId) headers['X-Tenant-Id'] = tenantId;
  return headers;
}

function buildUrl(agentId: string, taskId: string, options: StreamDeepResearchOptions): string {
  const params = new URLSearchParams();
  if (options.pollInterval !== undefined) params.set('poll_interval', String(options.pollInterval));
  if (options.deadlineSeconds !== undefined) params.set('deadline_seconds', String(options.deadlineSeconds));
  if (options.afterStep) params.set('after_step', String(options.afterStep));
  if (options.afterClaim) params.set('after_claim', String(options.afterClaim));
  if (options.afterSourceNote) params.set('after_source_note', String(options.afterSourceNote));
  if (options.afterLaneSummary) params.set('after_lane_summary', String(options.afterLaneSummary));
  if (options.afterReflection) params.set('after_reflection', String(options.afterReflection));
  if (options.afterControllerTrace) params.set('after_controller_trace', String(options.afterControllerTrace));
  const query = params.toString();
  const suffix = query ? `?${query}` : '';
  return `${API_BASE}/deep-research/stream/${encodeURIComponent(agentId)}/${encodeURIComponent(taskId)}${suffix}`;
}

function parseSseFrames(buffer: string): { events: DeepResearchStreamEvent[]; remainder: string } {
  const events: DeepResearchStreamEvent[] = [];
  const frames = buffer.split('\n\n');
  const remainder = frames.pop() ?? '';
  for (const frame of frames) {
    const trimmed = frame.trim();
    if (!trimmed.startsWith('data:')) continue;
    const payload = trimmed.slice('data:'.length).trim();
    if (!payload) continue;
    try {
      events.push(JSON.parse(payload) as DeepResearchStreamEvent);
    } catch {
      // Drop malformed frames silently — the server may send heartbeat-only
      // intervals that aren't JSON. Real errors arrive as `event: 'error'`.
    }
  }
  return { events, remainder };
}

/**
 * Async generator over the SSE event stream. Caller consumes via `for await`:
 *
 *   for await (const event of streamDeepResearch(agentId, taskId)) {
 *     if (event.event === 'final') break;
 *     // ...render incremental UI...
 *   }
 *
 * The generator terminates when the server emits a `final` event, when
 * `options.signal` aborts, or when the underlying response closes.
 */
export async function* streamDeepResearch(
  agentId: string,
  taskId: string,
  options: StreamDeepResearchOptions = {},
): AsyncGenerator<DeepResearchStreamEvent, void, unknown> {
  const fetchFn = options.fetchImpl ?? fetch;
  const url = buildUrl(agentId, taskId, options);
  const response = await fetchFn(url, {
    method: 'GET',
    headers: authHeaders(),
    signal: options.signal,
  });

  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = await response.text();
      detail = body || detail;
    } catch {
      // Ignore — fall back to status code message
    }
    throw new ApiError(response.status, detail);
  }

  if (!response.body) {
    throw new ApiError(0, 'SSE response has no readable body');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const { events, remainder } = parseSseFrames(buffer);
      buffer = remainder;
      for (const event of events) {
        yield event;
        if (event.event === 'final') {
          return;
        }
      }
    }
    // Flush any trailing frame that did not end with \n\n
    if (buffer.trim()) {
      const { events } = parseSseFrames(buffer + '\n\n');
      for (const event of events) {
        yield event;
        if (event.event === 'final') return;
      }
    }
  } finally {
    try {
      reader.releaseLock();
    } catch {
      // Best-effort release; safe to ignore
    }
  }
}

export const deepResearchApi = { streamDeepResearch };
