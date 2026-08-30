// @vitest-environment jsdom

import React from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/core';

const mocks = vi.hoisted(() => ({
  queryCalls: [] as Array<{ key: unknown[]; enabled: unknown }>,
  listSessions: vi.fn(),
  getSessionTranscript: vi.fn(),
  getSessionMessages: vi.fn(),
  getSessionLineage: vi.fn(),
  navigate: vi.fn(),
  sessionId: 'foreign-session' as string | undefined,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key,
    i18n: { language: 'en' },
  }),
}));

vi.mock('@tanstack/react-query', () => ({
  useQuery: (options: { queryKey: unknown[]; enabled?: unknown }) => {
    mocks.queryCalls.push({ key: options.queryKey, enabled: options.enabled });
    if (String(options.queryKey[0]) === 'agent') {
      return {
        data: {
          id: 'agent-1',
          name: 'EventPilot',
          status: 'running',
          agent_type: 'cloud',
          access_level: 'manage',
        },
        isLoading: false,
        error: null,
      };
    }
    return {
      data: undefined,
      dataUpdatedAt: 0,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    };
  },
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}));

vi.mock('react-router-dom', () => ({
  useParams: () => ({ id: 'agent-1', sessionId: mocks.sessionId }),
  useLocation: () => ({
    hash: '',
    search: '',
    pathname: mocks.sessionId
      ? `/agents/agent-1/sessions/${mocks.sessionId}`
      : '/agents/agent-1',
    state: null,
  }),
  useNavigate: () => mocks.navigate,
}));

vi.mock('../stores', () => {
  const state = {
    token: 'signed-in-token',
    user: { id: 'platform-admin-1', role: 'platform_admin' },
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
  },
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
  default: () => <div data-testid="fabricated-session-shell">Read-only · User</div>,
}));

import AgentDetail from './AgentDetail';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.queryCalls.length = 0;
  mocks.sessionId = 'foreign-session';
  mocks.listSessions.mockResolvedValue([]);
  mocks.getSessionMessages.mockResolvedValue([]);
  mocks.getSessionLineage.mockResolvedValue([]);
});

afterEach(cleanup);

describe('AgentDetail direct-session authority presentation', () => {
  it.each([
    [403, 'Session access denied'],
    [404, 'Session not found'],
  ] as const)('keeps an unknown route resolving, then renders the %s terminal without a Session shell', async (status, title) => {
    const transcript = deferred<never[]>();
    mocks.getSessionTranscript.mockReturnValue(transcript.promise);

    const view = render(<AgentDetail />);
    await waitFor(() => expect(mocks.getSessionTranscript).toHaveBeenCalledTimes(1));

    const shellWhilePending = screen.queryByTestId('fabricated-session-shell');
    const resolvingWhilePending = screen.queryByText('Resolving session…');

    await act(async () => {
      transcript.reject(new ApiError(status, title));
    });

    const alert = await screen.findByRole('alert');
    expect(resolvingWhilePending).toBeTruthy();
    expect(shellWhilePending).toBeNull();
    expect(alert.textContent).toContain(title);
    expect(screen.queryByTestId('fabricated-session-shell')).toBeNull();
    expect(mocks.getSessionMessages).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Back to digital employees' }));
    expect(mocks.navigate).toHaveBeenCalledWith('/agents', { replace: true });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(mocks.getSessionTranscript).toHaveBeenCalledTimes(1);
    mocks.sessionId = undefined;
    view.rerender(<AgentDetail />);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('replaces the resolving state with the verified read-only Session after a successful lookup', async () => {
    mocks.getSessionTranscript.mockResolvedValue([]);

    render(<AgentDetail />);

    expect(await screen.findByTestId('fabricated-session-shell')).toBeTruthy();
    expect(screen.queryByText('Resolving session…')).toBeNull();
    expect(mocks.getSessionMessages).toHaveBeenCalledTimes(1);
  });
});
