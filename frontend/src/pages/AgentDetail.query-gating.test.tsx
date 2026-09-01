import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mockState = vi.hoisted(() => ({
  queryCalls: [] as Array<{
    key: unknown[];
    enabled: unknown;
    refetchInterval?: unknown;
    refetchOnWindowFocus?: unknown;
  }>,
  hash: '#aware',
  accessLevel: 'use',
  operatorCap: false,
  userRole: 'member',
  locationState: null as null | Record<string, unknown>,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key,
    i18n: { language: 'en' },
  }),
}));

vi.mock('@tanstack/react-query', () => ({
  useQuery: (options: {
    queryKey: unknown[];
    enabled?: unknown;
    refetchInterval?: unknown;
    refetchOnWindowFocus?: unknown;
  }) => {
    mockState.queryCalls.push({
      key: options.queryKey,
      enabled: options.enabled,
      refetchInterval: options.refetchInterval,
      refetchOnWindowFocus: options.refetchOnWindowFocus,
    });
    const key = String(options.queryKey[0]);
    if (key === 'agent') {
      return {
        data: {
          id: 'agent-aware',
          access_level: mockState.accessLevel,
          action_capabilities: { can_operator_inspect: mockState.operatorCap },
        },
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
  useLocation: () => ({ hash: mockState.hash, search: '', pathname: '/agents/agent-aware', state: mockState.locationState }),
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
vi.mock('./agent-detail/AgentChatSection', () => ({
  default: ({ sessionTransitionPending }: { sessionTransitionPending?: boolean }) => (
    <div data-testid="agent-chat-section" data-session-transition-pending={sessionTransitionPending ? 'true' : 'false'} />
  ),
}));
vi.mock('./agent-detail/AgentMindSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentSettingsSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentExtensionsSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentStatusSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentWorkspaceSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentA2ASection', () => ({ default: () => null }));

import AgentDetail from './AgentDetail';

describe('AgentDetail aware reflection session gating', () => {
  beforeEach(() => {
    mockState.queryCalls.length = 0;
    mockState.hash = '#aware';
    mockState.accessLevel = 'use';
    mockState.operatorCap = false;
    mockState.userRole = 'member';
    mockState.locationState = null;
  });

  it('disables reflection sessions query for non-managers on the aware tab', () => {
    renderToStaticMarkup(<AgentDetail />);

    const reflectionQuery = mockState.queryCalls.find(
      (entry) => entry.key[0] === 'reflection-sessions',
    );

    expect(reflectionQuery?.enabled).toBe(false);
  });

  it('does not treat generic manage access as operator inspection authority', () => {
    mockState.accessLevel = 'manage';

    renderToStaticMarkup(<AgentDetail />);

    const reflectionQuery = mockState.queryCalls.find(
      (entry) => entry.key[0] === 'reflection-sessions',
    );

    expect(reflectionQuery?.enabled).toBe(false);
  });

  it('surfaces the operator reason control only for server-authorized inspectors', () => {
    mockState.operatorCap = true;

    const html = renderToStaticMarkup(<AgentDetail />);

    expect(html).toContain('data-testid="agent-operator-reason"');
  });

  it('renders an operator-only read shell without owner or mutation tabs and header actions', () => {
    mockState.accessLevel = 'operator';
    mockState.operatorCap = true;
    mockState.hash = '#chat';

    const html = renderToStaticMarkup(<AgentDetail />);

    expect(html).toContain('>Chat<');
    expect(html).toContain('Overview');
    expect(html).toContain('Conversation &amp; Tasks');
    expect(html).toContain('Documents &amp; Workspace');
    expect(html).not.toContain('>Status<');
    expect(html).not.toContain('>Memory<');
    expect(html).not.toContain('>Workflows<');
    expect(html).not.toContain('>Settings<');
    expect(html).not.toContain('agent-detail-header-actions');

    const permissionQuery = mockState.queryCalls.find(
      (entry) => JSON.stringify(entry.key) === JSON.stringify(['agent-permissions', 'agent-aware']),
    );
    expect(permissionQuery?.enabled).toBe(false);

    const agentQuery = mockState.queryCalls.find((entry) => entry.key[0] === 'agent');
    expect(agentQuery?.refetchOnWindowFocus).toBe(true);
    expect(typeof agentQuery?.refetchInterval).toBe('function');
    const refetchInterval = agentQuery?.refetchInterval as (
      query: { state: { data: { access_level?: string } } },
    ) => number | false;
    expect(refetchInterval({ state: { data: { access_level: 'operator' } } })).toBe(30_000);
    expect(refetchInterval({ state: { data: { access_level: 'use' } } })).toBe(false);
  });

  it('loads agent permissions on the chat tab for the composer permission badge', () => {
    mockState.hash = '#chat';

    renderToStaticMarkup(<AgentDetail />);

    const permissionQuery = mockState.queryCalls.find(
      (entry) => JSON.stringify(entry.key) === JSON.stringify(['agent-permissions', 'agent-aware']),
    );

    expect(permissionQuery?.enabled).toBe(true);
  });

  it('withdraws the old Session surface on the first render of a new-conversation navigation', () => {
    mockState.hash = '#chat';
    mockState.locationState = {
      newSessionDraft: {
        agent_id: 'agent-aware',
        request_id: 'new-session-request-1',
      },
    };

    const markup = renderToStaticMarkup(<AgentDetail />);

    expect(markup).toContain('data-session-transition-pending="true"');
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
    expect(markup).toContain('Extensions');
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
