import { useEffect, useRef } from 'react';
import type { QueryClient } from '@tanstack/react-query';
import type { ChatSession, SessionRun } from '../../api/domains/chat';
import { SIDEBAR_CONVERSATIONS_QUERY_KEY } from '../layout/sidebarConversationQuery';
import { TERMINAL_RUNTIME_PHASES, type RuntimePhase } from './chatRuntime';

export function useSidebarConversationRefresh(
  queryClient: QueryClient,
  userId: string | undefined,
  session: Pick<ChatSession, 'id' | 'audience_kind' | 'operator_view'> | null,
  run: SessionRun | null | undefined,
  phase: RuntimePhase,
) {
  const sessionId = session?.audience_kind !== 'group' && !session?.operator_view ? session?.id : undefined;
  const runState = sessionId && run ? `${sessionId}:${run.run_id}:${run.status}` : run === null ? null : undefined;
  const liveBoundary = sessionId && (TERMINAL_RUNTIME_PHASES.has(phase) || phase === 'awaiting_approval')
    ? `${sessionId}:${phase}` : null;
  const previousRun = useRef<string | null>(null);
  const previousBoundary = useRef<string | null>(null);

  useEffect(() => {
    // A short run can end before runs/active observes it. The existing live
    // phase supplies that boundary, including pauses for a decision.
    const observedChange = runState !== undefined && previousRun.current !== runState;
    const reachedBoundary = liveBoundary !== null && previousBoundary.current !== liveBoundary;
    if (runState !== undefined) previousRun.current = runState;
    previousBoundary.current = liveBoundary;
    if (userId && (observedChange || reachedBoundary)) {
      void queryClient.invalidateQueries({ queryKey: [...SIDEBAR_CONVERSATIONS_QUERY_KEY, userId] });
    }
  }, [queryClient, userId, runState, liveBoundary]);
}
