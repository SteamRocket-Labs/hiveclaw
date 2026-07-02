import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mockState = vi.hoisted(() => ({
  queryCalls: [] as Array<{ key: unknown[]; enabled: unknown; refetchInterval?: unknown }>,
  hash: '#aware',
  accessLevel: 'use',
  userRole: 'member',
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key,
    i18n: { language: 'en' },
  }),
}));

vi.mock('@tanstack/react-query', () => ({
  useQuery: (options: { queryKey: unknown[]; enabled?: unknown; refetchInterval?: unknown }) => {
    mockState.queryCalls.push({
      key: options.queryKey,
      enabled: options.enabled,
      refetchInterval: options.refetchInterval,
    });
    const key = String(options.queryKey[0]);
    if (key === 'agent') {
      return {
        data: { id: 'agent-aware', access_level: mockState.accessLevel },
        isLoading: false,
        isError: false,
        error: null,
      };
    }
    return {
      data: [],
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    };
  },
  useQueryClient: () => ({
    invalidateQueries: vi.fn(),
  }),
}));

vi.mock('react-router-dom', () => ({
  useParams: () => ({ id: 'agent-aware' }),
  useLocation: () => ({ hash: mockState.hash, search: '', pathname: '/agents/agent-aware' }),
  useNavigate: () => vi.fn(),
}));

vi.mock('../stores', () => {
  const state = {
    token: 'token',
    user: {
      id: 'user-1',
      role: mockState.userRole,
    },
  };
  const useAuthStore = ((selector: (input: typeof state) => unknown) => selector(state)) as unknown as typeof import('../stores').useAuthStore;
  Object.assign(useAuthStore, {
    getState: () => state,
  });
  return { useAuthStore };
});

vi.mock('../components/ConfirmModal', () => ({ default: () => null }));
vi.mock('../components/FileBrowser', () => ({ default: () => null }));
vi.mock('../components/PromptModal', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentApprovalsSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentActivityLogSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentAwareSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentChatSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentMindSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentSettingsSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentSkillsSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentStatusSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentWorkspaceSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentA2ASection', () => ({ default: () => null }));
vi.mock('./agent-detail/ToolsManager', () => ({ default: () => null }));

import AgentDetail from './AgentDetail';

describe('AgentDetail aware reflection session gating', () => {
  beforeEach(() => {
    mockState.queryCalls.length = 0;
    mockState.hash = '#aware';
    mockState.accessLevel = 'use';
    mockState.userRole = 'member';
  });

  it('disables reflection sessions query for non-managers on the aware tab', () => {
    renderToStaticMarkup(<AgentDetail />);

    const reflectionQuery = mockState.queryCalls.find(
      (entry) => JSON.stringify(entry.key) === JSON.stringify(['reflection-sessions', 'agent-aware']),
    );

    expect(reflectionQuery?.enabled).toBe(false);
  });

  it('keeps reflection sessions query enabled for managers on the aware tab', () => {
    mockState.accessLevel = 'manage';

    renderToStaticMarkup(<AgentDetail />);

    const reflectionQuery = mockState.queryCalls.find(
      (entry) => JSON.stringify(entry.key) === JSON.stringify(['reflection-sessions', 'agent-aware']),
    );

    expect(reflectionQuery?.enabled).toBe(true);
  });

  it('loads agent permissions on the chat tab for the composer permission badge', () => {
    mockState.hash = '#chat';

    renderToStaticMarkup(<AgentDetail />);

    const permissionQuery = mockState.queryCalls.find(
      (entry) => JSON.stringify(entry.key) === JSON.stringify(['agent-permissions', 'agent-aware']),
    );

    expect(permissionQuery?.enabled).toBe(true);
  });

  it('renders product workbench areas while preserving legacy hash routing', () => {
    mockState.accessLevel = 'manage';
    mockState.hash = '#tools';

    const markup = renderToStaticMarkup(<AgentDetail />);

    expect(markup).toContain('Overview');
    expect(markup).toContain('Conversation &amp; Tasks');
    expect(markup).toContain('Capabilities');
    expect(markup).toContain('Memory &amp; Knowledge');
    expect(markup).toContain('A2A / Team');
    expect(markup).toContain('Documents &amp; Workspace');
    expect(markup).toContain('Permissions &amp; Settings');
    expect(markup).toContain('Tools');
    expect(markup).toContain('Skills');
    expect(markup).toContain('Workflows');
  });
});

describe('AgentDetail chat runtime polling convergence (plan D1)', () => {
  beforeEach(() => {
    mockState.queryCalls.length = 0;
    mockState.hash = '#chat';
    mockState.accessLevel = 'use';
    mockState.userRole = 'member';
  });

  it('registers chat-active-run with a live-gated refetchInterval function, not an unconditional timer', () => {
    renderToStaticMarkup(<AgentDetail />);

    const activeRunCall = mockState.queryCalls.find((call) => String(call.key[0]) === 'chat-active-run');
    expect(activeRunCall).toBeTruthy();
    const interval = (activeRunCall as { refetchInterval?: unknown } | undefined)?.refetchInterval;
    expect(typeof interval).toBe('function');
    const intervalFn = interval as (query: { state: { data: unknown } }) => number | false;
    expect(intervalFn({ state: { data: null } })).toBe(false);
    expect(intervalFn({ state: { data: { status: 'completed' } } })).toBe(false);
    expect(intervalFn({ state: { data: { status: 'running' } } })).toBe(3000);
    expect(intervalFn({ state: { data: { status: 'pending' } } })).toBe(3000);
  });
});
