import { useEffect } from 'react';
import { useQueryClient, type QueryClient } from '@tanstack/react-query';
import type { AgentChatMessage } from './chatRuntime';

/**
 * The identity of the newest approval-completed session event in a timeline.
 * Empty string when none is present. The approval card's query key is prefixed
 * with ['session-approval', agentId, sessionId]; see InlineApprovalCard.
 */
export function approvalResolutionEventId(messages: readonly AgentChatMessage[]): string {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.eventType === 'permission_resolved' || message.eventType === 'session_permission_decision'
      || message.eventStatus === 'session_permission_decision') {
      return message.transcriptEventId || message.id || message.messageId || '';
    }
  }
  return '';
}

/**
 * A decision made in another window must still reach a requester whose waiting
 * line stopped polling: the existing approval-completed session event
 * invalidates the session-approval query. No new polling is added.
 */
export function useApprovalResolutionRefresh(
  agentId: string | null | undefined,
  sessionId: string | number | null | undefined,
  messages: readonly AgentChatMessage[],
  clientOverride?: QueryClient,
) {
  const contextClient = useQueryClient();
  const client = clientOverride ?? contextClient;
  const eventId = approvalResolutionEventId(messages);
  useEffect(() => {
    if (!eventId || !agentId || sessionId === undefined || sessionId === null || sessionId === '') return;
    void client.invalidateQueries({ queryKey: ['session-approval', agentId, sessionId] });
  }, [client, agentId, sessionId, eventId]);
}
