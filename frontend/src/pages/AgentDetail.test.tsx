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
  sessionPermissionModeFromSession,
} from './AgentDetail';

describe('AgentDetail session permission state', () => {
  it('uses the persisted agent preference for new conversations', () => {
    expect(defaultSessionPermissionModeFromAgent({ default_session_permission_mode: 'auto' })).toBe('auto');
    expect(defaultSessionPermissionModeFromAgent(
      { default_session_permission_mode: 'bypassPermissions' },
    )).toBe('bypassPermissions');
    expect(defaultSessionPermissionModeFromAgent({ default_session_permission_mode: 'unknown' })).toBe('default');
  });

  it('restores the composer permission mode from persisted session metadata', () => {
    expect(
      sessionPermissionModeFromSession({
        id: 'session-1',
        permission_mode: 'bypassPermissions',
      }),
    ).toBe('bypassPermissions');

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
        id: 'draft:session-6',
        is_draft: true,
        permission_mode: 'bypassPermissions',
      }),
    ).toBe('bypassPermissions');
  });

  it('activates full access as a session mode with a simple risk acknowledgement', async () => {
    const source = await readSource('./AgentDetail.tsx');

    expect(source).toContain("persistSessionPermissionMode('bypassPermissions')");
    expect(source).toContain('Full access skips routine session approval prompts');
    expect(source).toContain('await ensureDurableActiveSession()');
    expect(source).not.toContain('break_glass_');
    expect(source).not.toContain('useSessionPermissionExpiry');
    expect(source).not.toContain('SessionFullAccessPrompt');
  });
});

describe('AgentDetail realtime refresh contract', () => {
  it('seals terminal websocket events onto the visible live process without rebuilding gapped history', async () => {
    const source = await readSource('./agent-detail/sessionSocketEventProjector.ts');
    const applierSource = await readSource('./agent-detail/sessionTranscriptApplier.ts');

    expect(source).toContain('applyTranscriptToSession(agentId, sessionId, transcriptEvent, isActiveRuntime)');
    expect(source).not.toContain('void selectSession(session)');
    expect(source).not.toContain("parseChatMsg({ role: 'assistant', content: `⚠️ ${message}` })");
    // The consumption contract moved to the extracted production applier; its
    // behavioral proof lives in sessionTranscriptApplier.test.ts.
    expect(applierSource).toContain('return envelopeApplication');
    expect(applierSource).toContain('mergeTranscriptBackfill(');
    expect(applierSource).toContain('events: appliedCanonicalEvents');
    expect(applierSource).toContain('consumed.application ?? false');
    expect(applierSource).toContain('mergeCanonicalTerminalMessages(previous, messages, runId)');
    expect(applierSource).not.toContain('transcriptEvents: nextTranscriptEvents');
    expect(applierSource).toContain('onTerminal: (runId) => deps.markActiveRunTerminal(key, runId)');
    // Compatibility carriers legacy-project only after contiguous application.
    expect(applierSource).toContain('if (!application) return false');
  });

  it('hydrates the complete canonical Session V2 transcript without a manual older-message gate', async () => {
    const source = await readSource('./AgentDetail.tsx');
    const sectionSource = await readSource('./agent-detail/AgentChatSection.tsx');

    expect(source).toContain('loadCanonicalSessionTranscript');
    expect(source).not.toContain("direction: 'backward'");
    expect(source).not.toContain('loadOlderMessages');
    expect(sectionSource).not.toContain('load-older-messages');
  });

  it('opens live transport from the first safe newest suffix while older history keeps recovering', async () => {
    const source = await readSource('./AgentDetail.tsx');

    expect(source).toContain('canonicalHydrationInFlight = canonicalHydration.liveReady');
    expect(source).toContain('return liveSubscriptionWatermark(projected.store)');
    expect(source).not.toContain('canonicalHydrationInFlight = canonicalHydration.then');
  });

  it('does not make REST transcript hydration a prerequisite for the live Session transport', async () => {
    const pageSource = await readSource('./AgentDetail.tsx');
    const controllerSource = await readSource('./agent-detail/useSessionTransportController.ts');

    expect(pageSource).not.toContain('transportHydratedKeys');
    expect(controllerSource).not.toContain('await optionsRef.current.callbacks.onBackfill(session, agentId)');
    expect(controllerSource).toContain('onLiveTailReady');
    expect(controllerSource).toContain('getLiveSubscriptionCursor');
  });

  it('never gives up transient reconnects and recovers missed durable transcript events', async () => {
    const source = await readSource('./agent-detail/useSessionTransportController.ts');
    const pageSource = await readSource('./AgentDetail.tsx');

    expect(source).not.toContain('attempts >= 20');
    expect(source).not.toContain('Giving up reconnect');
    expect(source).not.toContain('event.code !== 1000');
    expect(source).toContain('const reconnect = shouldReconnectSessionSocket(');
    expect(source).toContain('if (reconnect) scheduleReconnect()');
    expect(source).toContain('reconnectDelayMs(previousAttempts)');
    expect(source).toContain("window.addEventListener('online', wake)");
    expect(source).toContain("window.addEventListener('offline', handleOffline)");
    expect(source).toContain("document.addEventListener('visibilitychange', handleVisibility)");
    expect(source).toContain('transportPollIntervalMs(');
    expect(source).toContain('backfill: () => optionsRef.current.callbacks.onBackfill(activeSession, agentId)');
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
