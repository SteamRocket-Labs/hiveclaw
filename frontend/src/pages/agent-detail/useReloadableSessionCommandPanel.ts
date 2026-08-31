import React from 'react';
import { useQuery } from '@tanstack/react-query';

import { ccParityApi } from '../../api/domains/ccParity';
import type { SessionCommandControlState } from './AgentChatSection';
import { commandResultRecord, getSessionCommandUiAction } from './sessionCommandResult';
import {
  readSessionCommandPanel,
  type SessionCommandPanelRoute,
} from './agentDetailPolicy';

export function sessionPanelCommandForUiAction(type: string): SessionCommandPanelRoute | null {
  if (type === 'open_context_panel') return 'context';
  if (type === 'open_usage_panel') return 'usage';
  if (type === 'open_permissions_menu') return 'permissions';
  return null;
}

export function useReloadableSessionCommandPanel({
  agentId,
  routeSessionId,
  activeSessionId,
  search,
}: {
  agentId?: string;
  routeSessionId?: string;
  activeSessionId?: string | null;
  search: string;
}): { routedSessionCommand: SessionCommandPanelRoute | null; control: SessionCommandControlState | null } {
  const routedSessionCommand = readSessionCommandPanel(search);
  const { data: response, error } = useQuery({
    queryKey: ['session-command-panel', agentId, activeSessionId, routedSessionCommand],
    queryFn: () => ccParityApi.executeCommand(agentId!, routedSessionCommand!, {
      arguments: {},
      session_id: activeSessionId,
    }),
    enabled: Boolean(
      agentId
      && routeSessionId
      && activeSessionId === routeSessionId
      && routedSessionCommand,
    ),
    retry: false,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });
  const control = React.useMemo<SessionCommandControlState | null>(() => {
    if (!routedSessionCommand) return null;
    const type = routedSessionCommand === 'context'
      ? 'context_panel'
      : routedSessionCommand === 'usage' ? 'usage_panel' : 'permissions_panel';
    const title = routedSessionCommand === 'context'
      ? 'Session context'
      : routedSessionCommand === 'usage' ? 'Session usage' : 'Session permissions';
    if (error) {
      return {
        type,
        title,
        message: `Could not reload this panel. Run /${routedSessionCommand} again.`,
        command: routedSessionCommand,
        level: 'error',
      };
    }
    if (!response) return null;
    const uiAction = getSessionCommandUiAction(response);
    if (sessionPanelCommandForUiAction(uiAction?.type || '') !== routedSessionCommand) return null;
    return {
      type,
      title,
      message: typeof uiAction?.message === 'string' ? uiAction.message : undefined,
      command: response.command,
      payload: commandResultRecord(response),
      level: uiAction?.level === 'error' ? 'error' : 'info',
    };
  }, [error, response, routedSessionCommand]);
  return { routedSessionCommand, control };
}
