import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mockState = vi.hoisted(() => ({
  queryCalls: [] as Array<{ key: unknown[]; enabled: unknown }>,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => key.split('.').pop() ?? key,
  }),
}));

vi.mock('@tanstack/react-query', () => ({
  useQuery: (options: { queryKey: unknown[]; enabled?: unknown }) => {
    mockState.queryCalls.push({ key: options.queryKey, enabled: options.enabled });
    const key = String(options.queryKey[0]);
    if (key === 'agents') {
      return {
        data: [
          { id: 'agent-1', name: 'Primary Bot', role_description: 'Main agent' },
          { id: 'agent-2', name: 'Reviewer Bot', role_description: 'Quality reviewer' },
        ],
      };
    }
    if (key === 'users') {
      return { data: [] };
    }
    return { data: [] };
  },
  useQueryClient: () => ({
    invalidateQueries: vi.fn(),
  }),
}));

vi.mock('../../stores', () => {
  const state = {
    user: {
      id: 'user-1',
      username: 'member',
      display_name: 'Member User',
      email: 'member@example.com',
    },
  };
  const useAuthStore = ((selector: (input: typeof state) => unknown) => selector(state)) as unknown as typeof import('../../stores').useAuthStore;
  Object.assign(useAuthStore, {
    getState: () => state,
  });
  return { useAuthStore };
});

vi.stubGlobal('localStorage', {
  getItem: vi.fn(() => 'tenant-1'),
  setItem: vi.fn(),
  removeItem: vi.fn(),
  clear: vi.fn(),
  key: vi.fn(),
  length: 0,
} as unknown as Storage);

import RelationshipEditor from './RelationshipEditor';

describe('RelationshipEditor read-only access', () => {
  beforeEach(() => {
    mockState.queryCalls.length = 0;
  });

  it('does not fetch admin-only users or render owner binding actions in read-only mode', () => {
    const markup = renderToStaticMarkup(
      <RelationshipEditor
        agentId="agent-1"
        agent={{ id: 'agent-1', access_level: 'use', owner_user_id: null }}
        readOnly
      />,
    );

    const usersQuery = mockState.queryCalls.find(
      (entry) => JSON.stringify(entry.key) === JSON.stringify(['users', 'tenant-1']),
    );

    expect(usersQuery?.enabled).toBe(false);
    expect(markup).not.toContain('bindEmployee');
    expect(markup).not.toContain('unbind');
    expect(markup).toContain('Reviewer Bot');
  });
});
