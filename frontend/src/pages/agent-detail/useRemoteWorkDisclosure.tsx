import React, { useState } from 'react';
import RemoteA2AWorkControls from '../../components/RemoteA2AWorkControls';

type RemoteWorkScope = {
  /** Stable per-session identity; the panel state resets when it changes. */
  identity: string;
  operatorView: boolean;
  effectiveAgentId: string | null;
  activeSession: { id?: string | number; user_id?: string; audience_kind?: string } | null | undefined;
  isDraftSession: boolean;
  canUseComposer: boolean;
  agent: { deactivation_reason?: string | null } | null | undefined;
  currentUser: { id?: string } | null | undefined;
};

/**
 * Remote A2A tasks are on demand (retrospective §7.3, Remote A2A row): nothing
 * remote renders on the default chat surface, but a conversation with an
 * existing remote task keeps reply / cancel / recover-files reachable behind
 * the composer "+" menu, under the same usability frame as before the trim.
 */
export function useRemoteWorkDisclosure(scope: RemoteWorkScope) {
  const [state, setState] = useState<{ scope: string; open: boolean } | null>(null);
  const open = state?.scope === scope.identity && state.open;
  const requestOpen = React.useCallback(() => setState({ scope: scope.identity, open: true }), [scope.identity]);
  const available = !scope.operatorView && Boolean(scope.effectiveAgentId && scope.activeSession?.id && !scope.isDraftSession)
    && (scope.canUseComposer || (scope.agent?.deactivation_reason === 'retirement_pending'
      && (scope.activeSession?.user_id === scope.currentUser?.id || scope.activeSession?.audience_kind === 'group')));
  return {
    available,
    open,
    requestOpen,
    element: open && scope.effectiveAgentId && scope.activeSession?.id ? (
      <RemoteA2AWorkControls key={`remote-work:${scope.identity}`} agentId={scope.effectiveAgentId}
        sessionId={String(scope.activeSession.id)} initialOpen
        onClose={() => setState({ scope: scope.identity, open: false })} />
    ) : null,
  };
}
