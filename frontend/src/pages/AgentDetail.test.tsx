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
vi.mock('./agent-detail/AgentSkillsSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentStatusSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentWorkspaceSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentA2ASection', () => ({ default: () => null }));
vi.mock('./agent-detail/ToolsManager', () => ({ default: () => null }));

import AgentDetail, { sessionPermissionModeFromSession } from './AgentDetail';

describe('AgentDetail session permission state', () => {
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
    ).toBe('bypassPermissions');
  });
});

describe('AgentDetail realtime refresh contract', () => {
  it('refreshes durable session history after terminal websocket events', async () => {
    const source = await readSource('./AgentDetail.tsx');

    expect(source).toContain('isTerminalRealtimeChatEvent');
    expect(source).toContain('applyTranscriptToSession(agentId, sessionId, transcriptEvent, isActiveRuntime)');
    expect(source).toContain('if (isActiveRuntime && isTerminalRealtimeChatEvent(transcriptEvent))');
    expect(source).toContain('void selectSession(sess)');
  });

  it('keeps the initial transcript read window slim', async () => {
    const source = await readSource('./AgentDetail.tsx');

    expect(source).toContain('const TRANSCRIPT_INITIAL_WINDOW = 25;');
    expect(source).toContain('const TRANSCRIPT_OLDER_PAGE = 50;');
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

    expect(getEnabled(['activity', 'agent-403'])).toBe(false);
    expect(getEnabled(['agent-capability-installs', 'agent-403'])).toBe(false);
    expect(getEnabled(['agent-channel-capabilities', 'agent-403'])).toBe(false);
    expect(getEnabled(['metrics', 'agent-403'])).toBe(false);
  });
});
