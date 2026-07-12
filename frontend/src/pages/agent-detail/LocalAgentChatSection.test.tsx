import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';

import type { LocalAgentChannelEvent } from '../../api/domains/localBridge';
import LocalAgentChatSection, {
  localAgentRuntimeResumeHealth,
  localAgentArtifactDownloadUrl,
  localAgentChannelEventsToChatMessages,
} from './LocalAgentChatSection';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback: string) => fallback,
  }),
}));

describe('LocalAgentChatSection local-channel projection', () => {
  it('projects durable local channel events into ordinary chat messages with artifacts', () => {
    const events: LocalAgentChannelEvent[] = [
      {
        id: 'event-user-1',
        sequence: 1,
        session_id: 'channel-session-1',
        message_id: 'message-1',
        direction: 'hive_to_local',
        type: 'message',
        payload: { content: '证明你是本地 agent，并上传 proof md' },
        created_at: '2026-06-25T01:00:00Z',
      },
      {
        id: 'event-local-1',
        sequence: 2,
        session_id: 'channel-session-1',
        message_id: 'message-1',
        direction: 'local_to_hive',
        type: 'result',
        payload: {
          output: '我是本地 Codex agent，文件已上传。',
          artifacts: [
            {
              path: 'workspace/uploads/hive-local-agent-proof.md',
              name: 'hive-local-agent-proof.md',
              preview_kind: 'markdown',
              size: 16,
            },
          ],
        },
        created_at: '2026-06-25T01:00:05Z',
      },
      {
        id: 'event-local-file',
        sequence: 3,
        session_id: 'channel-session-1',
        message_id: null,
        direction: 'local_to_hive',
        type: 'file',
        payload: {
          path: 'workspace/uploads/agent-note.md',
          name: 'agent-note.md',
          text: '我是agent',
        },
        created_at: '2026-06-25T01:00:06Z',
      },
    ];

    const messages = localAgentChannelEventsToChatMessages(events);

    expect(messages).toMatchObject([
      {
        id: 'event-user-1',
        role: 'user',
        content: '证明你是本地 agent，并上传 proof md',
      },
      {
        id: 'event-local-1',
        role: 'assistant',
        content: '我是本地 Codex agent，文件已上传。',
        artifacts: [
          {
            name: 'hive-local-agent-proof.md',
            path: 'workspace/uploads/hive-local-agent-proof.md',
            previewKind: 'markdown',
            size: 16,
            source: 'local_agent',
          },
        ],
      },
      {
        id: 'event-local-file',
        role: 'assistant',
        content: '我是agent',
        artifacts: [
          {
            name: 'agent-note.md',
            path: 'workspace/uploads/agent-note.md',
            source: 'local_agent',
          },
        ],
      },
    ]);
  });

  it('builds credential-free local artifact paths for authenticated fetch', () => {
    expect(
      localAgentArtifactDownloadUrl('workspace/local-bridge/result.md', {
        agentId: 'agent-local-1',
        sessionId: 'channel-session-1',
      }),
    ).toBe(
      '/api/agents/agent-local-1/local-agent/sessions/channel-session-1/workspace/download?path=workspace%2Flocal-bridge%2Fresult.md',
    );
    expect(localAgentArtifactDownloadUrl('workspace/uploads/proof.md')).toBe(
      '/api/local-agents/workspace/download?path=workspace%2Fuploads%2Fproof.md',
    );
  });

  it('collapses local delta and result events for one cloud request into one assistant message', () => {
    const events: LocalAgentChannelEvent[] = [
      {
        id: 'event-user-2',
        sequence: 1,
        session_id: 'channel-session-1',
        message_id: 'message-2',
        direction: 'hive_to_local',
        type: 'message',
        payload: { content: '证明你是本地 agent' },
        created_at: '2026-06-25T02:00:00Z',
      },
      {
        id: 'event-delta-1',
        sequence: 2,
        session_id: 'channel-session-1',
        message_id: 'message-2',
        direction: 'local_to_hive',
        type: 'delta',
        payload: { text: '我是' },
        created_at: '2026-06-25T02:00:01Z',
      },
      {
        id: 'event-delta-2',
        sequence: 3,
        session_id: 'channel-session-1',
        message_id: 'message-2',
        direction: 'local_to_hive',
        type: 'delta',
        payload: { text: 'agent' },
        created_at: '2026-06-25T02:00:02Z',
      },
      {
        id: 'event-result-2',
        sequence: 4,
        session_id: 'channel-session-1',
        message_id: 'message-2',
        direction: 'local_to_hive',
        type: 'result',
        payload: { output: '我是agent。' },
        created_at: '2026-06-25T02:00:03Z',
      },
    ];

    const messages = localAgentChannelEventsToChatMessages(events);

    expect(messages).toHaveLength(2);
    expect(messages[1]).toMatchObject({
      id: 'event-result-2',
      role: 'assistant',
      content: '我是agent。',
    });
  });

  it('derives runtime health from Hive Connect presence instead of browser live-channel state', () => {
    expect(
      localAgentRuntimeResumeHealth(
        [
          {
            id: 'conn-1',
            tenant_id: 'tenant-1',
            user_id: 'user-1',
            device_name: 'MacBook.local',
            client_kind: 'hive-connect',
            status: 'active',
            presence_status: 'offline',
            scopes: [],
          },
        ],
        false,
      ),
    ).toBe('offline');

    expect(localAgentRuntimeResumeHealth(undefined, true)).toBe('unknown');

    expect(
      localAgentRuntimeResumeHealth(
        [
          {
            id: 'conn-2',
            tenant_id: 'tenant-1',
            user_id: 'user-1',
            device_name: 'MacBook.local',
            client_kind: 'hive-connect',
            status: 'active',
            presence_status: 'online',
            scopes: [],
          },
        ],
        false,
      ),
    ).toBe('online');
  });

  it('renders the local agent composer with the same Codex-style control surface as ordinary chat sessions', () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    const markup = renderToStaticMarkup(
      <MemoryRouter initialEntries={['/agents/agent-local-1?session_id=chat-session-1#chat']}>
        <QueryClientProvider client={queryClient}>
          <LocalAgentChatSection
            agentId="agent-local-1"
            agent={{ id: 'agent-local-1', name: 'Codex on Mac' }}
            agentPermissions={{
              scope_type: 'agent',
              access_level: 'manage',
            }}
          />
        </QueryClientProvider>
      </MemoryRouter>,
    );

    expect(markup).toContain('data-testid="local-agent-session-composer"');
    // Full-height flex-column layout now lives in the .local-chat-root class
    // (height:100% migrated off inline style) — asserts the Codex-style layout
    // is applied, not the legacy fixed calc height.
    expect(markup).toContain('local-chat-root');
    expect(markup).not.toContain('height:calc(100vh - 206px)');
    expect(markup).toContain('data-testid="session-composer-shell"');
    expect(markup).toContain('data-testid="session-composer-plus-menu"');
    expect(markup).toContain('Upload file');
    expect(markup).toContain('Plan Mode');
    expect(markup).not.toContain('Goal mode');
    expect(markup).toContain('Scheduled task');
    expect(markup).toContain('data-testid="session-composer-action-plan-switch"');
    expect(markup).not.toContain('data-testid="session-composer-action-goal-switch"');
    expect(markup).not.toContain('data-testid="session-composer-action-schedule-switch"');
    expect(markup).toContain('role="switch"');
    expect(markup).toContain('aria-checked="false"');
    expect(markup).toContain('Manage access');
    expect(markup).toContain('Hive Connect');
    expect(markup).not.toContain('aria-label="Attach file"');
    expect(markup).not.toContain('microphone');
    expect(markup).not.toContain('Voice');
  });
});
