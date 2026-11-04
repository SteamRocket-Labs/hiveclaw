// @vitest-environment jsdom

import React from 'react';
import { act, cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  // The route's bot id changes between rerenders the way a sidebar click does:
  // same route component, new useParams().id, no remount.
  agentId: 'agent-a' as string,
  sessionId: undefined as string | undefined,
  hash: '#chat' as string,
  search: '' as string,
  listSessions: vi.fn(),
  getSessionTranscript: vi.fn(),
  getSessionMessages: vi.fn(),
  getSessionLineage: vi.fn(),
  navigate: vi.fn(),
  chatProps: null as any,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key,
    i18n: { language: 'en' },
  }),
}));

vi.mock('@tanstack/react-query', () => ({
  // Both bots' records are already cached, exactly like production where the
  // member had opened every bot before switching between them.
  useQuery: (options: { queryKey: unknown[] }) => {
    if (options.queryKey[0] === 'agent') {
      return {
        data: {
          id: mocks.agentId,
          name: mocks.agentId === 'agent-a' ? 'Orchestrator' : 'Empty Bot',
          status: 'running',
          agent_type: 'cloud',
          access_level: 'use',
          action_capabilities: { can_manage: false, can_manage_permissions: false, can_operator_inspect: false },
        },
        isLoading: false,
        error: null,
      };
    }
    return { data: undefined, dataUpdatedAt: 0, isLoading: false, error: null, refetch: vi.fn() };
  },
  useQueryClient: () => ({
    invalidateQueries: vi.fn(),
    cancelQueries: vi.fn(),
    removeQueries: vi.fn(),
  }),
}));

vi.mock('react-router-dom', () => ({
  useParams: () => ({ id: mocks.agentId, sessionId: mocks.sessionId }),
  useLocation: () => ({
    hash: mocks.hash,
    search: mocks.search,
    pathname: `/agents/${mocks.agentId}`,
    state: null,
  }),
  useNavigate: () => mocks.navigate,
  useInRouterContext: () => true,
  Link: ({ children }: { children: React.ReactNode }) => <a href="#chat">{children}</a>,
}));

vi.mock('../stores', () => {
  const state = {
    token: 'signed-in-token',
    get user() { return { id: 'member-1', role: 'member' }; },
  };
  const useAuthStore = ((selector: (input: typeof state) => unknown) => selector(state)) as unknown as typeof import('../stores').useAuthStore;
  Object.assign(useAuthStore, { getState: () => state });
  return { useAuthStore };
});

vi.mock('../api/domains/chat', () => ({
  chatApi: {
    listSessions: mocks.listSessions,
    getSessionTranscript: mocks.getSessionTranscript,
    getSessionMessages: mocks.getSessionMessages,
    getSessionLineage: mocks.getSessionLineage,
    startSessionRun: vi.fn(),
    createSessionRun: vi.fn(),
    cancelSessionRun: vi.fn(),
    createSession: vi.fn(),
  },
}));

vi.mock('../api/domains/activity', () => ({
  activityApi: { list: vi.fn().mockResolvedValue([]), getToolFailureSummary: vi.fn().mockResolvedValue(undefined) },
}));

vi.mock('./agent-detail/useSessionTransportController', () => ({
  useSessionTransportController: () => ({
    wsConnected: false,
    transportPhase: 'initializing',
    transportReconnectAttempt: 0,
    closeSessionSocket: vi.fn(),
    getSessionSocket: vi.fn(() => null),
    reconnectActiveTransport: vi.fn(),
    resetActiveTransportState: vi.fn(),
    syncActiveSocketState: vi.fn(),
  }),
}));

vi.mock('../components/ConfirmModal', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentApprovalsSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentActivityLogSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentAwareSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentEvolutionSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentExtensionsSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentKnowledgeSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentBusinessTasksSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentSettingsSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentWorkspaceSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentWorkflowsSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentA2ASection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentStatusSection', () => ({ default: () => null }));
vi.mock('./LocalAgents', () => ({ default: () => null }));
vi.mock('./agent-detail/LocalAgentChatSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentChatSection', () => ({
  default: (props: { [key: string]: any }) => {
    mocks.chatProps = props;
    return <div data-testid="fabricated-session-shell" />;
  },
}));

import AgentDetail from './AgentDetail';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const sessionOf = (agentId: string, id: string, title: string) => ({
  id,
  agent_id: agentId,
  user_id: 'member-1',
  title,
  session_kind: 'human_chat',
  source_channel: 'web',
  read_only: false,
  operator_view: false,
  last_message_at: `2026-11-0${id.endsWith('1') ? '4' : '3'}T10:00:00Z`,
});

const agentASessions = [sessionOf('agent-a', 's-a-1', 'Latest A'), sessionOf('agent-a', 's-a-2', 'Older A')];

beforeEach(() => {
  vi.clearAllMocks();
  mocks.agentId = 'agent-a';
  mocks.sessionId = undefined;
  mocks.hash = '#chat';
  mocks.search = '';
  mocks.chatProps = null;
  mocks.getSessionTranscript.mockResolvedValue([]);
  mocks.getSessionMessages.mockResolvedValue([]);
  mocks.getSessionLineage.mockResolvedValue([]);
  mocks.listSessions.mockImplementation((agentId: string, scope: string) => {
    if (scope === 'mine') return Promise.resolve(agentId === 'agent-a' ? agentASessions : []);
    return Promise.resolve([]);
  });
});

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

/** Every session-scoped read that actually executed, as (agentId, sessionId). */
function executedSessionReads(): Array<[string, string]> {
  const reads: Array<[string, string]> = [];
  for (const call of mocks.getSessionTranscript.mock.calls) reads.push([String(call[0]), String(call[1])]);
  for (const call of mocks.getSessionMessages.mock.calls) reads.push([String(call[0]), String(call[1])]);
  for (const call of mocks.getSessionLineage.mock.calls) reads.push([String(call[0]), String(call[1])]);
  return reads;
}

const sessionOwner: Record<string, string> = { 's-a-1': 'agent-a', 's-a-2': 'agent-a', 's-b-1': 'agent-b' };

/** Reads pairing one bot's id with another bot's conversation id. */
function crossBotReads(): Array<[string, string]> {
  return executedSessionReads().filter(([agentId, sessionId]) => (
    sessionOwner[sessionId] !== undefined && sessionOwner[sessionId] !== agentId
  ));
}

function switchToBot(view: { rerender: (ui: React.ReactElement) => void }, agentId: string) {
  mocks.agentId = agentId;
  act(() => { view.rerender(<AgentDetail />); });
}

describe('returning to a bot after a bot switch (B5-fix2)', () => {
  it("A → empty B → A selects A's latest conversation and lists A's history", async () => {
    const view = render(<AgentDetail />);
    await waitFor(() => expect(mocks.chatProps?.activeSession?.id).toBe('s-a-1'));
    expect(mocks.chatProps.sessions.map((session: { id: string }) => session.id)).toEqual(['s-a-1', 's-a-2']);

    // B has no conversations: B5 opens a fresh 新对话 draft on it.
    switchToBot(view, 'agent-b');
    await waitFor(() => expect(mocks.chatProps?.activeSession?.id).toMatch(/^draft:/));

    // Going back to A must land in A's own latest conversation again, not the
    // stale draft screen, and 历史 must list A's conversations.
    switchToBot(view, 'agent-a');
    await waitFor(() => expect(mocks.chatProps?.activeSession?.id).toBe('s-a-1'), { timeout: 3000 });
    expect(mocks.chatProps.sessions.map((session: { id: string }) => session.id)).toEqual(['s-a-1', 's-a-2']);
    expect(mocks.getSessionTranscript.mock.calls.some(([agentId, sessionId]) => agentId === 'agent-a' && sessionId === 's-a-1')).toBe(true);
  });

  it("A → B (both with conversations) → A reopens A's latest without any cross-bot read", async () => {
    mocks.listSessions.mockImplementation((agentId: string, scope: string) => {
      if (scope !== 'mine') return Promise.resolve([]);
      return Promise.resolve(agentId === 'agent-a' ? agentASessions : [sessionOf('agent-b', 's-b-1', 'Latest B')]);
    });
    const view = render(<AgentDetail />);
    await waitFor(() => expect(mocks.chatProps?.activeSession?.id).toBe('s-a-1'));

    switchToBot(view, 'agent-b');
    await waitFor(() => expect(mocks.chatProps?.activeSession?.id).toBe('s-b-1'), { timeout: 3000 });

    switchToBot(view, 'agent-a');
    await waitFor(() => expect(mocks.chatProps?.activeSession?.id).toBe('s-a-1'), { timeout: 3000 });

    // No executed read ever pairs one bot's id with another bot's conversation.
    expect(crossBotReads()).toEqual([]);
    // Each bot's own reads went to its own conversation.
    expect(executedSessionReads()).toEqual(expect.arrayContaining([['agent-a', 's-a-1'], ['agent-b', 's-b-1']]));
  });

  it('clears the previous bot’s conversation before any session-scoped read can pair it with the new bot', async () => {
    const view = render(<AgentDetail />);
    await waitFor(() => expect(mocks.chatProps?.activeSession?.id).toBe('s-a-1'));

    // Watch every session-scoped effect (lineage, transcript, messages) for a
    // request that combines agent-b with A's conversation.
    switchToBot(view, 'agent-b');
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });

    const mixed = executedSessionReads().filter(([agentId, sessionId]) => agentId === 'agent-b' && sessionId.startsWith('s-a-'));
    expect(mixed).toEqual([]);
  });
});
