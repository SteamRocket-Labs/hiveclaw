import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { taskApi } from './tasks';

const localStore: Record<string, string> = {};
const localStorageStub = {
  getItem: (key: string) => localStore[key] ?? null,
  setItem: (key: string, value: string) => {
    localStore[key] = value;
  },
  removeItem: (key: string) => {
    delete localStore[key];
  },
  clear: () => {
    for (const key of Object.keys(localStore)) delete localStore[key];
  },
};

let fetchMock: ReturnType<typeof vi.fn<typeof fetch>>;

beforeEach(() => {
  vi.stubGlobal('localStorage', localStorageStub);
  localStorageStub.setItem('token', 'test-token');
  fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
    new Response(JSON.stringify({ status: 'triggered' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }),
  );
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  localStorageStub.clear();
});

describe('taskApi durable request identity', () => {
  it('preserves the caller request id when creating a business task', async () => {
    await taskApi.create('agent-1', {
      request_id: 'create-request-1',
      title: 'Research',
      description: 'Inspect evidence',
    });

    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(JSON.parse(String(init.body))).toMatchObject({
      request_id: 'create-request-1',
      title: 'Research',
    });
  });

  it('sends the stable request id and plan provenance for a manual trigger', async () => {
    await taskApi.trigger('agent-1', 'task-1', {
      request_id: 'trigger-request-1',
      confirmed_plan_id: 'plan-1',
      confirmed_plan_version: 2,
      confirmed_plan_hash: 'sha256:plan',
    });

    const call = fetchMock.mock.calls[0];
    expect(String(call?.[0])).toBe('/api/agents/agent-1/tasks/task-1/trigger');
    expect(JSON.parse(String((call?.[1] as RequestInit).body))).toEqual({
      request_id: 'trigger-request-1',
      confirmed_plan_id: 'plan-1',
      confirmed_plan_version: 2,
      confirmed_plan_hash: 'sha256:plan',
    });
  });
});
