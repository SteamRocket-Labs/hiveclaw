import { describe, expect, it, vi } from 'vitest';

import type { LocalAgentChannelEvent } from '../../api/domains/localBridge';
import {
  localAgentArtifactDownloadUrl,
  localAgentChannelEventsToChatMessages,
} from './LocalAgentChatSection';

describe('LocalAgentChatSection local-channel projection', () => {
  it('projects durable local channel events into ordinary chat messages with artifacts', () => {
    const events: LocalAgentChannelEvent[] = [
      {
        id: 'event-user-1',
        session_id: 'channel-session-1',
        message_id: 'message-1',
        direction: 'hive_to_local',
        type: 'message',
        payload: { content: '证明你是本地 agent，并上传 proof md' },
        created_at: '2026-06-25T01:00:00Z',
      },
      {
        id: 'event-local-1',
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

  it('downloads local artifacts from the user-scoped local workspace, not the agent workspace', () => {
    vi.stubGlobal('localStorage', {
      getItem: vi.fn(() => 'token-1'),
    });

    expect(localAgentArtifactDownloadUrl('workspace/uploads/proof.md')).toBe(
      '/api/local-agents/workspace/download?path=workspace%2Fuploads%2Fproof.md&token=token-1',
    );
  });

  it('collapses local delta and result events for one cloud request into one assistant message', () => {
    const events: LocalAgentChannelEvent[] = [
      {
        id: 'event-user-2',
        session_id: 'channel-session-1',
        message_id: 'message-2',
        direction: 'hive_to_local',
        type: 'message',
        payload: { content: '证明你是本地 agent' },
        created_at: '2026-06-25T02:00:00Z',
      },
      {
        id: 'event-delta-1',
        session_id: 'channel-session-1',
        message_id: 'message-2',
        direction: 'local_to_hive',
        type: 'delta',
        payload: { text: '我是' },
        created_at: '2026-06-25T02:00:01Z',
      },
      {
        id: 'event-delta-2',
        session_id: 'channel-session-1',
        message_id: 'message-2',
        direction: 'local_to_hive',
        type: 'delta',
        payload: { text: 'agent' },
        created_at: '2026-06-25T02:00:02Z',
      },
      {
        id: 'event-result-2',
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
});
