import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const queryCalls: Array<{ key: unknown[]; enabled: unknown }> = [];

async function readSource(relativePath: string): Promise<string> {
  const fsModuleId = 'node:fs';
  const { readFileSync } = (await import(/* @vite-ignore */ fsModuleId)) as {
    readFileSync: (path: URL, encoding: string) => string;
  };
  return readFileSync(new URL(relativePath, import.meta.url), 'utf8');
}

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key,
    i18n: { language: 'en' },
  }),
}));

vi.mock('@tanstack/react-query', () => ({
  useQuery: (options: { queryKey: unknown[]; enabled?: unknown }) => {
    queryCalls.push({ key: options.queryKey, enabled: options.enabled });
    const key = String(options.queryKey[0]);
    if (key === 'agent') {
      return {
        data: undefined,
        isLoading: false,
        isError: true,
        error: { status: 403, message: 'Access denied' },
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
  useParams: () => ({ id: 'agent-403' }),
  useLocation: () => ({ hash: '', search: '', pathname: '/agents/agent-403' }),
  useNavigate: () => vi.fn(),
}));

vi.mock('../stores', () => {
  const state = {
    token: null,
    user: null,
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
vi.mock('./agent-detail/AgentExtensionsSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentStatusSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentWorkspaceSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentA2ASection', () => ({ default: () => null }));

import AgentDetail, {
  defaultSessionPermissionModeFromAgent,
  sessionBreakGlassExpiryDelay,
  sessionPermissionModeFromSession,
} from './AgentDetail';

describe('AgentDetail session permission state', () => {
  it('uses the persisted agent preference for new conversations', () => {
    expect(defaultSessionPermissionModeFromAgent({ default_session_permission_mode: 'auto' })).toBe('auto');
    expect(defaultSessionPermissionModeFromAgent(
      { default_session_permission_mode: 'bypassPermissions' },
      true,
    )).toBe('bypassPermissions');
    expect(defaultSessionPermissionModeFromAgent(
      { default_session_permission_mode: 'bypassPermissions' },
      false,
    )).toBe('default');
    expect(defaultSessionPermissionModeFromAgent({ default_session_permission_mode: 'unknown' })).toBe('default');
  });

  it('computes the remaining full-access lifetime for UI downgrade scheduling', () => {
    expect(sessionBreakGlassExpiryDelay({
      permission_mode: 'bypassPermissions',
      break_glass: { expires_at: '2026-07-13T12:01:00Z' },
    }, Date.parse('2026-07-13T12:00:00Z'))).toBe(60_000);
    expect(sessionBreakGlassExpiryDelay({ permission_mode: 'auto' }, 0)).toBeNull();
  });

  it('restores the composer permission mode from persisted session metadata', () => {
    expect(
      sessionPermissionModeFromSession({
        id: 'session-1',
        permission_mode: 'bypassPermissions',
      }),
    ).toBe('default');

    expect(
      sessionPermissionModeFromSession({
        id: 'session-2',
        permission_profile: { mode: 'default' },
      }),
    ).toBe('default');

    expect(
      sessionPermissionModeFromSession({
        id: 'session-3',
        transcript_metadata_json: { permission_mode: 'auto' },
      }),
    ).toBe('auto');

    expect(
      sessionPermissionModeFromSession({
        id: 'session-4',
      }),
    ).toBe('default');

    expect(
      sessionPermissionModeFromSession({
        id: 'session-5',
        permission_mode: 'bypassPermissions',
        break_glass: {
          operator_id: 'admin-1',
          reason: 'incident response',
          scope: 'session',
          expires_at: '2999-01-01T00:00:00Z',
        },
      }),
    ).toBe('bypassPermissions');

    expect(
      sessionPermissionModeFromSession({
        id: 'draft:session-6',
        is_draft: true,
        permission_mode: 'bypassPermissions',
      }),
    ).toBe('bypassPermissions');
  });

  it('activates full access through a scoped, expiring break-glass update', async () => {
    const source = await readSource('./AgentDetail.tsx');

    expect(source).toContain("break_glass_scope: 'session'");
    expect(source).toContain('break_glass_reason: reason');
    expect(source).toContain('break_glass_ttl_minutes: 60');
    expect(source).toContain('await ensureDurableActiveSession()');
    expect(source).toContain(
      'isDraftHumanChatSession(activeSession) ? DEFAULT_SESSION_PERMISSION_MODE : previous',
    );
    expect(source).not.toContain('setSessionPermissionMode(previous)');
  });

  it('surfaces a server-sync failure when a full-access grant expires', async () => {
    const source = await readSource('./agent-detail/SessionPermissionLifecycle.tsx');

    expect(source).not.toContain('.catch(() => undefined)');
    expect(source).toContain("agent.chat.permission.expirySyncFailed");
  });
});

describe('AgentDetail realtime refresh contract', () => {
  it('refreshes durable session history after terminal websocket events', async () => {
    const source = await readSource('./agent-detail/sessionSocketEventProjector.ts');

    expect(source).toContain('isTerminalRealtimeChatEvent');
    expect(source).toContain('applyTranscriptToSession(agentId, sessionId, transcriptEvent, isActiveRuntime)');
    expect(source).toContain('if (isActiveRuntime && isTerminalRealtimeChatEvent(transcriptEvent))');
    expect(source).toContain('void selectSession(session)');
  });

  it('keeps the initial transcript read window slim', async () => {
    const source = await readSource('./agent-detail/agentDetailPolicy.ts');

    expect(source).toContain('export const TRANSCRIPT_INITIAL_WINDOW = 25;');
    expect(source).toContain('export const TRANSCRIPT_OLDER_PAGE = 50;');
  });

  it('never gives up transient reconnects and recovers missed durable transcript events', async () => {
    const source = await readSource('./agent-detail/useSessionTransportController.ts');
    const pageSource = await readSource('./AgentDetail.tsx');

    expect(source).not.toContain('attempts >= 20');
    expect(source).not.toContain('Giving up reconnect');
    expect(source).toContain('reconnectDelayMs(previousAttempts)');
    expect(source).toContain("window.addEventListener('online', wake)");
    expect(source).toContain("window.addEventListener('offline', handleOffline)");
    expect(source).toContain("document.addEventListener('visibilitychange', handleVisibility)");
    expect(source).toContain('transportPollIntervalMs(');
    expect(source).toContain('await optionsRef.current.callbacks.onBackfill(activeSession, agentId)');
    expect(pageSource).toContain('onReconnectTransport={reconnectActiveTransport}');
  });
});

describe('AgentDetail access failures', () => {
  beforeEach(() => {
    queryCalls.length = 0;
  });

  it('stops status-side queries and shows a forbidden message when the main agent query returns 403', () => {
    const markup = renderToStaticMarkup(<AgentDetail />);

    expect(markup).toContain('You do not have access to this employee.');

    const getEnabled = (queryKey: unknown[]) =>
      queryCalls.find((entry) => JSON.stringify(entry.key) === JSON.stringify(queryKey))?.enabled;

    expect(getEnabled(['activity', 'agent-403', 'owner'])).toBe(false);
    expect(getEnabled(['agent-capability-installs', 'agent-403'])).toBe(false);
    expect(getEnabled(['agent-channel-capabilities', 'agent-403'])).toBe(false);
    expect(getEnabled(['metrics', 'agent-403'])).toBe(false);
  });
});
