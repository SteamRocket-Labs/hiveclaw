import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { streamDeepResearch, type DeepResearchStreamEvent } from './deepResearch';

// Node test environment has no localStorage; stub a minimal in-memory backing.
const _localStore: Record<string, string> = {};
const _localStorageStub = {
  getItem: (key: string) => (key in _localStore ? _localStore[key] : null),
  setItem: (key: string, value: string) => {
    _localStore[key] = value;
  },
  removeItem: (key: string) => {
    delete _localStore[key];
  },
  clear: () => {
    for (const key of Object.keys(_localStore)) delete _localStore[key];
  },
};

type FetchMock = ReturnType<typeof vi.fn<typeof fetch>>;
type FetchHeaders = Record<string, string>;

function fetchHeadersOf(stub: FetchMock, callIndex = 0): FetchHeaders {
  const call = stub.mock.calls[callIndex];
  const init = call?.[1] as RequestInit | undefined;
  return (init?.headers ?? {}) as FetchHeaders;
}

function fetchUrlOf(stub: FetchMock, callIndex = 0): string {
  const call = stub.mock.calls[callIndex];
  return String(call?.[0] ?? '');
}

function makeSseBody(frames: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const frame of frames) {
        controller.enqueue(encoder.encode(frame));
      }
      controller.close();
    },
  });
}

function makeResponse(body: ReadableStream<Uint8Array>, status = 200): Response {
  return new Response(body, {
    status,
    headers: { 'content-type': 'text/event-stream' },
  });
}

describe('streamDeepResearch (Tier 3-4)', () => {
  beforeEach(() => {
    vi.stubGlobal('localStorage', _localStorageStub);
    _localStorageStub.clear();
    _localStorageStub.setItem('token', 'test-token');
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('parses SSE frames and yields typed events until final', async () => {
    const frames = [
      'data: ' + JSON.stringify({ event: 'step', task_id: 't1', payload: { phase: 'plan' } }) + '\n\n',
      'data: ' + JSON.stringify({ event: 'claim', task_id: 't1', payload: { claim_id: 'c1' } }) + '\n\n',
      'data: ' + JSON.stringify({ event: 'report', task_id: 't1', payload: { markdown: '# Brief', chars: 7 } }) + '\n\n',
      'data: ' + JSON.stringify({ event: 'final', task_id: 't1', payload: { status: 'completed' } }) + '\n\n',
    ];
    const fetchStub = vi.fn(async () => makeResponse(makeSseBody(frames)));

    const events: DeepResearchStreamEvent[] = [];
    for await (const event of streamDeepResearch('agent-1', 't1', { fetchImpl: fetchStub })) {
      events.push(event);
    }

    expect(events.map((e) => e.event)).toEqual(['step', 'claim', 'report', 'final']);
    expect(events[events.length - 1].payload.status).toBe('completed');

    // URL + Authorization header
    expect(fetchStub).toHaveBeenCalledTimes(1);
    expect(fetchUrlOf(fetchStub)).toContain('/api/deep-research/stream/agent-1/t1');
    expect(fetchHeadersOf(fetchStub)).toMatchObject({
      Accept: 'text/event-stream',
      Authorization: 'Bearer test-token',
    });
  });

  it('terminates as soon as a final event arrives even if more frames follow', async () => {
    const frames = [
      'data: ' + JSON.stringify({ event: 'claim', task_id: 't2', payload: {} }) + '\n\n',
      'data: ' + JSON.stringify({ event: 'final', task_id: 't2', payload: { status: 'failed' } }) + '\n\n',
      // Extra frame after final must NOT be yielded
      'data: ' + JSON.stringify({ event: 'heartbeat', task_id: 't2', payload: {} }) + '\n\n',
    ];
    const fetchStub = vi.fn(async () => makeResponse(makeSseBody(frames)));

    const events: DeepResearchStreamEvent[] = [];
    for await (const event of streamDeepResearch('agent-1', 't2', { fetchImpl: fetchStub })) {
      events.push(event);
    }
    expect(events.map((e) => e.event)).toEqual(['claim', 'final']);
  });

  it('handles frames that arrive split across chunks', async () => {
    // Split a single JSON event across two chunks to exercise the buffer remainder logic.
    const payload = JSON.stringify({ event: 'claim', task_id: 't3', payload: { claim_id: 'c-split' } });
    const finalFrame = 'data: ' + JSON.stringify({ event: 'final', task_id: 't3', payload: { status: 'completed' } }) + '\n\n';
    const half = Math.floor(payload.length / 2);
    const frames = [
      'data: ' + payload.slice(0, half),
      payload.slice(half) + '\n\n' + finalFrame,
    ];
    const fetchStub = vi.fn(async () => makeResponse(makeSseBody(frames)));

    const events: DeepResearchStreamEvent[] = [];
    for await (const event of streamDeepResearch('agent-1', 't3', { fetchImpl: fetchStub })) {
      events.push(event);
    }
    expect(events.map((e) => e.event)).toEqual(['claim', 'final']);
    expect(events[0].payload.claim_id).toBe('c-split');
  });

  it('forwards cursor query params and tenant header when set', async () => {
    _localStorageStub.setItem('current_tenant_id', 'tenant-9');
    const fetchStub = vi.fn(async () => makeResponse(makeSseBody([
      'data: ' + JSON.stringify({ event: 'final', task_id: 't4', payload: {} }) + '\n\n',
    ])));

    for await (const _ of streamDeepResearch('agent-1', 't4', {
      pollInterval: 0.25,
      deadlineSeconds: 30,
      afterClaim: 5,
      afterControllerTrace: 2,
      fetchImpl: fetchStub,
    })) {
      // consume
    }

    const calledUrl = fetchUrlOf(fetchStub);
    expect(calledUrl).toContain('poll_interval=0.25');
    expect(calledUrl).toContain('deadline_seconds=30');
    expect(calledUrl).toContain('after_claim=5');
    expect(calledUrl).toContain('after_controller_trace=2');
    expect(fetchHeadersOf(fetchStub)).toMatchObject({
      'X-Tenant-Id': 'tenant-9',
    });
  });

  it('throws ApiError when the response is not ok', async () => {
    const fetchStub = vi.fn(async () => new Response('forbidden', { status: 403 }));

    const consume = async () => {
      // eslint-disable-next-line @typescript-eslint/no-unused-vars
      for await (const _ of streamDeepResearch('agent-1', 'bad-task', { fetchImpl: fetchStub })) {
        // never reached
      }
    };

    await expect(consume()).rejects.toMatchObject({ status: 403 });
  });
});
