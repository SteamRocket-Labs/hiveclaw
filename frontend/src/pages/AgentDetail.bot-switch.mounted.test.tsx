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
  getRuntimeSummary: vi.fn(),
  getActiveSessionRun: vi.fn(),
  getSessionIndex: vi.fn(),
  listSessionDecisions: vi.fn(),
  getSessionWorkbench: vi.fn(),
  getSessionContextUsage: vi.fn(),
  navigate: vi.fn(),
  chatProps: null as any,
  // Session-scoped query keys the real components built, as
  // [prefix, agentId, sessionId] — the runtime-summary request itself carries
  // no agent id, so its pairing is only observable here.
  sessionQueryKeys: [] as Array<[string, string, string]>,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string | Record<string, unknown>, values?: Record<string, unknown>) => {
      const preset = typeof fallback === 'object' ? fallback?.defaultValue : undefined;
      const template: string = typeof fallback === 'string' ? fallback : typeof preset === 'string' ? preset : key;
      const interpolation = typeof fallback === 'object' ? fallback : values;
      return Object.entries(interpolation || {}).reduce<string>(
        (text, [name, value]) => text.replace(`{{${name}}}`, String(value)),
        template,
      );
    },
    i18n: { language: 'en' },
  }),
}));

// Session-scoped queries keep the real component code path: the component
// still decides `enabled`, builds the queryKey and the queryFn. The fake
// client subscribes through an effect, so a queryFn runs only for a render
// that actually committed — React throws away render passes discarded by a
// render-phase state adjustment, and a real observer never subscribes to
// those — so every recorded read is a read the live app would really send.
const SESSION_QUERY_PREFIXES = new Set([
  'chat-runtime-summary',
  'chat-active-run',
  'chat-session-workbench',
  'chat-session-index',
  'chat-session-decisions',
  'chat-session-context-usage',
]);
const executedQueryFetches = new Set<string>();

vi.mock('@tanstack/react-query', async () => {
  const { useEffect } = await vi.importActual<typeof import('react')>('react');
  return {
    useQuery: (options: { queryKey: unknown[]; enabled?: boolean; queryFn?: () => unknown }) => {
      const [prefix, agentId, sessionId] = options.queryKey as unknown[];
      // Both bots' records are already cached, exactly like production where the
      // member had opened every bot before switching between them.
      if (prefix === 'agent') {
        return {
          data: {
            id: String(agentId),
            name: agentId === 'agent-a' ? 'Orchestrator' : 'Empty Bot',
            status: 'running',
            agent_type: 'cloud',
            access_level: 'use',
            action_capabilities: { can_manage: false, can_manage_permissions: false, can_operator_inspect: false },
          },
          isLoading: false,
          error: null,
        };
      }
      if (SESSION_QUERY_PREFIXES.has(String(prefix))) {
        // The prefix is constant per call site, so this hook is unconditional
        // in React's eyes; the guard below re-checks the committed state.
        useEffect(() => {
          if (options.enabled === false || agentId == null || sessionId == null) return;
          const key = JSON.stringify([prefix, agentId, sessionId]);
          if (executedQueryFetches.has(key)) return;
          executedQueryFetches.add(key);
          mocks.sessionQueryKeys.push([String(prefix), String(agentId), String(sessionId)]);
          try {
            void Promise.resolve(options.queryFn?.()).catch(() => {});
          } catch { /* the recorded pairing is what this harness asserts */ }
        }, [JSON.stringify([prefix, agentId, sessionId])]);
      }
      return { data: undefined, dataUpdatedAt: 0, isLoading: false, error: null, refetch: vi.fn() };
    },
    useMutation: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), reset: vi.fn() }),
    useQueryClient: () => ({
      invalidateQueries: vi.fn(),
      cancelQueries: vi.fn(),
      removeQueries: vi.fn(),
    }),
  };
});

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
    getRuntimeSummary: mocks.getRuntimeSummary,
    getActiveSessionRun: mocks.getActiveSessionRun,
    getSessionIndex: mocks.getSessionIndex,
    listSessionDecisions: mocks.listSessionDecisions,
    startSessionRun: vi.fn(),
    createSessionRun: vi.fn(),
    cancelSessionRun: vi.fn(),
    createSession: vi.fn(),
  },
}));

vi.mock('../api/domains/ccParity', () => ({
  ccParityApi: {
    getSessionWorkbench: mocks.getSessionWorkbench,
    getSessionContextUsage: mocks.getSessionContextUsage,
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

// The real chat section renders so its four session-scoped queries really
// execute; the wrapper only observes the props AgentDetail passes down.
vi.mock('./agent-detail/AgentChatSection', async (importOriginal) => {
  const real = await importOriginal<typeof import('./agent-detail/AgentChatSection')>();
  const RealAgentChatSection = real.default as unknown as React.ComponentType<Record<string, unknown>>;
  return {
    default: (props: { [key: string]: any }) => {
      mocks.chatProps = props;
      return <RealAgentChatSection {...props} />;
    },
  };
});

import AgentDetail from './AgentDetail';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// jsdom has no layout: the real chat section scrolls on mount and on messages.
Element.prototype.scrollIntoView = () => {};
HTMLElement.prototype.scrollTo = () => {};

const sessionOf = (agentId: string, id: string, title: string, extra: Record<string, unknown> = {}) => ({
  id,
  agent_id: agentId,
  user_id: 'member-1',
  title,
  session_kind: 'human_chat',
  source_channel: 'web',
  read_only: false,
  operator_view: false,
  last_message_at: `2026-11-0${id.endsWith('1') ? '4' : '3'}T10:00:00Z`,
  ...extra,
});

let agentASessions = [sessionOf('agent-a', 's-a-1', 'Latest A'), sessionOf('agent-a', 's-a-2', 'Older A')];
let botBSessions: Array<ReturnType<typeof sessionOf>> = [];

/** The bot each conversation id really belongs to. */
let sessionOwner: Record<string, string> = { 's-a-1': 'agent-a', 's-a-2': 'agent-a', 's-b-1': 'agent-b' };

// Delaying bot B's list response keeps the switch's first frame open: nothing
// has re-selected a session yet, so a stale selection is fully exposed.
let holdBotBList = false;
let pendingBotBListResolvers: Array<() => void> = [];
const holdBotBListResponse = () => { holdBotBList = true; };
const settleBotBList = async () => {
  await act(async () => {
    const resolvers = pendingBotBListResolvers;
    pendingBotBListResolvers = [];
    holdBotBList = false;
    resolvers.forEach((resolve) => resolve());
    await Promise.resolve();
  });
};

beforeEach(() => {
  vi.clearAllMocks();
  mocks.agentId = 'agent-a';
  mocks.sessionId = undefined;
  mocks.hash = '#chat';
  mocks.search = '';
  mocks.chatProps = null;
  mocks.sessionQueryKeys = [];
  executedQueryFetches.clear();
  agentASessions = [sessionOf('agent-a', 's-a-1', 'Latest A'), sessionOf('agent-a', 's-a-2', 'Older A')];
  botBSessions = [];
  sessionOwner = { 's-a-1': 'agent-a', 's-a-2': 'agent-a', 's-b-1': 'agent-b' };
  holdBotBList = false;
  pendingBotBListResolvers = [];
  mocks.getSessionTranscript.mockResolvedValue([]);
  mocks.getSessionMessages.mockResolvedValue([]);
  mocks.getSessionLineage.mockResolvedValue([]);
  mocks.getRuntimeSummary.mockResolvedValue(null);
  mocks.getActiveSessionRun.mockResolvedValue(null);
  mocks.getSessionIndex.mockResolvedValue({ checkpoints: [] });
  mocks.listSessionDecisions.mockResolvedValue([]);
  mocks.getSessionWorkbench.mockResolvedValue({ schema: 'session_workbench.v1', runtime_sections: {} });
  mocks.getSessionContextUsage.mockResolvedValue(null);
  mocks.listSessions.mockImplementation((agentId: string, scope: string) => {
    if (scope !== 'mine') return Promise.resolve([]);
    if (agentId === 'agent-b' && holdBotBList) {
      return new Promise<typeof botBSessions>((resolve) => { pendingBotBListResolvers.push(() => resolve(botBSessions)); });
    }
    return Promise.resolve(agentId === 'agent-a' ? agentASessions : botBSessions);
  });
});

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

/** Every executed session-scoped read as (endpoint, agentId|null, sessionId). */
function endpointReads(): Array<[string, string | null, string]> {
  const reads: Array<[string, string | null, string]> = [];
  const push = (label: string, agent: unknown, session: unknown) => {
    if (agent != null && session != null) reads.push([label, String(agent), String(session)]);
  };
  for (const call of mocks.getSessionTranscript.mock.calls) push('transcript', call[0], call[1]);
  for (const call of mocks.getSessionMessages.mock.calls) push('messages', call[0], call[1]);
  for (const call of mocks.getSessionLineage.mock.calls) push('lineage', call[0], call[1]);
  for (const call of mocks.getActiveSessionRun.mock.calls) push('runs/active', call[0], call[1]);
  for (const call of mocks.getSessionIndex.mock.calls) push('index', call[0], call[1]);
  for (const call of mocks.listSessionDecisions.mock.calls) push('decisions', call[0], call[1]);
  for (const call of mocks.getSessionWorkbench.mock.calls) push('workbench', call[0], call[1]);
  for (const call of mocks.getSessionContextUsage.mock.calls) push('context-usage', call[0], call[1]);
  for (const call of mocks.getRuntimeSummary.mock.calls) push('runtime-summary', null, call[0]);
  return reads;
}

/** Reads (API calls and built query keys) that pair one bot's id with another bot's conversation. */
function crossBotReads(): string[] {
  const offenders: string[] = [];
  for (const [endpoint, agentId, sessionId] of endpointReads()) {
    if (agentId != null && sessionOwner[sessionId] !== undefined && sessionOwner[sessionId] !== agentId) {
      offenders.push(`${endpoint}: ${agentId} × ${sessionId}`);
    }
  }
  for (const [prefix, agentId, sessionId] of mocks.sessionQueryKeys) {
    if (sessionOwner[sessionId] !== undefined && sessionOwner[sessionId] !== agentId) {
      offenders.push(`${prefix} (query key): ${agentId} × ${sessionId}`);
    }
  }
  return offenders;
}

function switchToBot(view: { rerender: (ui: React.ReactElement) => void }, agentId: string) {
  mocks.agentId = agentId;
  act(() => { view.rerender(<AgentDetail />); });
}

describe('returning to a bot after a bot switch (B5-fix2)', () => {
  it("A → empty B → A selects A's latest conversation and lists A's history", async () => {
    const view = render(<AgentDetail />);
    await waitFor(() => expect(mocks.chatProps?.activeSession?.id).toBe('s-a-1'), { timeout: 10000 });
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
    expect(crossBotReads()).toEqual([]);
  });

  it('A → B (both with conversations, B’s list delayed) reads every endpoint with B’s own conversation only', async () => {
    botBSessions = [sessionOf('agent-b', 's-b-1', 'Latest B')];
    const view = render(<AgentDetail />);
    await waitFor(() => expect(mocks.chatProps?.activeSession?.id).toBe('s-a-1'), { timeout: 10000 });

    // Hold B's list back so the switch's first frame — the render right after
    // the route changed, before anything re-selected — is fully observed.
    holdBotBListResponse();
    switchToBot(view, 'agent-b');
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    expect(crossBotReads()).toEqual([]);

    await settleBotBList();
    await waitFor(() => expect(mocks.chatProps?.activeSession?.id).toBe('s-b-1'), { timeout: 3000 });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });

    // Every one of the seven round-1 endpoints was read for (B, B's latest).
    const readFor = (endpoint: string) => endpointReads().filter(([name, agentId, sessionId]) => (
      name === endpoint && agentId === 'agent-b' && sessionId === 's-b-1'
    ));
    expect(readFor('transcript')).toHaveLength(1);
    expect(readFor('messages')).toHaveLength(1);
    expect(readFor('lineage')).toHaveLength(1);
    expect(readFor('runs/active')).toHaveLength(1);
    expect(readFor('index')).toHaveLength(1);
    expect(readFor('decisions')).toHaveLength(1);
    expect(readFor('workbench')).toHaveLength(1);
    expect(readFor('context-usage')).toHaveLength(1);
    expect(mocks.getRuntimeSummary.mock.calls.some(([sessionId]) => sessionId === 's-b-1')).toBe(true);
    expect(mocks.sessionQueryKeys.some(([prefix, agentId, sessionId]) => (
      prefix === 'chat-runtime-summary' && agentId === 'agent-b' && sessionId === 's-b-1'
    ))).toBe(true);

    // And no read — on any endpoint, at any moment — paired a bot with a
    // foreign conversation.
    expect(crossBotReads()).toEqual([]);
  });

  it('clears the previous bot’s conversation before any of the seven reads can pair it with the new bot', async () => {
    botBSessions = [sessionOf('agent-b', 's-b-1', 'Latest B')];
    const view = render(<AgentDetail />);
    await waitFor(() => expect(mocks.chatProps?.activeSession?.id).toBe('s-a-1'), { timeout: 10000 });

    holdBotBListResponse();
    switchToBot(view, 'agent-b');
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });

    const mixedEndpoints = endpointReads().filter(([, agentId, sessionId]) => agentId === 'agent-b' && String(sessionId).startsWith('s-a-'));
    const mixedKeys = mocks.sessionQueryKeys.filter(([, agentId, sessionId]) => agentId === 'agent-b' && String(sessionId).startsWith('s-a-'));
    expect(mixedEndpoints).toEqual([]);
    expect(mixedKeys).toEqual([]);

    await settleBotBList();
    await waitFor(() => expect(mocks.chatProps?.activeSession?.id).toBe('s-b-1'), { timeout: 3000 });
    expect(crossBotReads()).toEqual([]);
  });

  it('B → A → B reopens B’s latest without any cross-bot read', async () => {
    botBSessions = [sessionOf('agent-b', 's-b-1', 'Latest B')];
    mocks.agentId = 'agent-b';
    const view = render(<AgentDetail />);
    await waitFor(() => expect(mocks.chatProps?.activeSession?.id).toBe('s-b-1'), { timeout: 3000 });

    switchToBot(view, 'agent-a');
    await waitFor(() => expect(mocks.chatProps?.activeSession?.id).toBe('s-a-1'), { timeout: 3000 });

    switchToBot(view, 'agent-b');
    await waitFor(() => expect(mocks.chatProps?.activeSession?.id).toBe('s-b-1'), { timeout: 3000 });

    expect(crossBotReads()).toEqual([]);
  });

  it('drops an A2A conversation whose peer is the new bot (peer access is not ownership)', async () => {
    // A's latest is an A2A session owned by A with peer B: the ownership
    // predicate accepts it for B, but the lineage route only matches
    // ChatSession.agent_id, so keeping it would 404 every read.
    agentASessions = [
      sessionOf('agent-a', 's-a-1', 'A2A latest', { peer_agent_id: 'agent-b', session_kind: 'agent_chat', source_channel: 'agent' }),
      sessionOf('agent-a', 's-a-2', 'Older A'),
    ];
    botBSessions = [sessionOf('agent-b', 's-b-1', 'Latest B')];

    const view = render(<AgentDetail />);
    await waitFor(() => expect(mocks.chatProps?.activeSession?.id).toBe('s-a-1'), { timeout: 10000 });

    switchToBot(view, 'agent-b');
    await waitFor(() => expect(mocks.chatProps?.activeSession?.id).toBe('s-b-1'), { timeout: 3000 });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });

    expect(crossBotReads()).toEqual([]);
  });

  it('drops a legacy conversation object without agent_id when the route changes', async () => {
    agentASessions = [
      sessionOf('agent-a', 's-a-1', 'Legacy latest', { agent_id: undefined }),
      sessionOf('agent-a', 's-a-2', 'Older A'),
    ];
    botBSessions = [sessionOf('agent-b', 's-b-1', 'Latest B')];

    const view = render(<AgentDetail />);
    await waitFor(() => expect(mocks.chatProps?.activeSession?.id).toBe('s-a-1'), { timeout: 10000 });

    switchToBot(view, 'agent-b');
    await waitFor(() => expect(mocks.chatProps?.activeSession?.id).toBe('s-b-1'), { timeout: 3000 });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });

    expect(crossBotReads()).toEqual([]);
  });
});
