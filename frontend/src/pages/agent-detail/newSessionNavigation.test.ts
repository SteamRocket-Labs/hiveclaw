import { describe, expect, it } from 'vitest';

import {
  buildNewSessionDraftNavigation,
  createDraftChatSession,
  readNewSessionDraftRequest,
} from './newSessionNavigation';

describe('new Session draft navigation', () => {
  it('binds one opaque request to the exact target agent', () => {
    const navigation = buildNewSessionDraftNavigation('agent/with space');

    expect(navigation.to).toBe('/agents/agent%2Fwith%20space#chat');
    expect(readNewSessionDraftRequest(navigation.state, 'agent/with space')).toEqual({
      agentId: 'agent/with space',
      requestId: navigation.state.newSessionDraft.request_id,
    });
    expect(navigation.state.newSessionDraft.request_id).not.toBe('');
  });

  it('rejects malformed and cross-agent navigation authority', () => {
    const navigation = buildNewSessionDraftNavigation('agent-1');

    expect(readNewSessionDraftRequest(navigation.state, 'agent-2')).toBeNull();
    expect(readNewSessionDraftRequest({ newSessionDraft: { agent_id: 'agent-1' } }, 'agent-1')).toBeNull();
    expect(readNewSessionDraftRequest(null, 'agent-1')).toBeNull();
  });

  it('gives repeated clicks distinct exact-once request identities', () => {
    const first = buildNewSessionDraftNavigation('agent-1');
    const second = buildNewSessionDraftNavigation('agent-1');

    expect(first.state.newSessionDraft.request_id).not.toBe(second.state.newSessionDraft.request_id);
  });

  it('builds a writable local draft without creating a durable Session', () => {
    const draft = createDraftChatSession({
      agentId: 'agent-1',
      userId: 'user-1',
      permissionMode: 'auto',
      now: new Date('2026-08-29T12:34:00Z'),
      requestId: 'request-1',
    });

    expect(draft).toMatchObject({
      id: 'draft:request-1',
      draft_client_id: 'draft:request-1',
      is_draft: true,
      agent_id: 'agent-1',
      user_id: 'user-1',
      source_channel: 'web',
      session_kind: 'human_chat',
      is_current_user_session: true,
      read_only: false,
      permission_mode: 'auto',
    });
  });
});
