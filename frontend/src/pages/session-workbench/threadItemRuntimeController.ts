import React from 'react';

import type { ThreadItem } from '../../api/domains/threadItems.generated';
import type { AgentChatMessage } from '../agent-detail/chatRuntime';

export type RuntimePanelAudience = 'user' | 'operator';

export function shouldDefaultCollapseRuntimePanel(
  viewportWidth: number,
  audience: RuntimePanelAudience = 'user',
): boolean {
  // Ordinary users start task-first at every width; the runtime rail opens on
  // demand. Operators explicitly came for evidence, so the rail stays open on
  // wide workbenches and yields only to narrow layouts.
  if (audience === 'operator') return viewportWidth <= 960;
  return true;
}

export function shouldRevealRuntimeInspectorForSelection(item: ThreadItem | null | undefined): boolean {
  return Boolean(item && item.audience === 'operator' && item.operator_details);
}

/** One explicit operator technical-details selection. A fresh request object is
 * issued for every explicit selection — including re-selecting the same item —
 * while transcript rebuilds that recreate the selected item issue none. */
export type RuntimeInspectorRevealRequest = {
  itemId: string;
};

/** An explicit operator technical-details click always reveals its inspector,
 * even when the rail was collapsed. Nothing else may force the rail open. */
export function useOperatorInspectorReveal(
  revealRequest: RuntimeInspectorRevealRequest | null,
  setCollapsed: React.Dispatch<React.SetStateAction<boolean>>,
): void {
  React.useEffect(() => {
    if (revealRequest) setCollapsed(false);
  }, [revealRequest, setCollapsed]);
}

export function useResponsiveRuntimePanel(
  audience: RuntimePanelAudience = 'user',
): [boolean, React.Dispatch<React.SetStateAction<boolean>>] {
  const [collapsed, setCollapsed] = React.useState(() => (
    shouldDefaultCollapseRuntimePanel(
      typeof window !== 'undefined' ? window.innerWidth : Number.POSITIVE_INFINITY,
      audience,
    )
  ));
  // The audience can resolve after mount (operator authority/session load) or
  // migrate on a session switch. Apply the resolved audience default once per
  // real migration; stable-audience rerenders and background events keep the
  // user's manual choice.
  const resolvedAudienceRef = React.useRef(audience);
  React.useEffect(() => {
    if (resolvedAudienceRef.current === audience) return;
    resolvedAudienceRef.current = audience;
    setCollapsed(shouldDefaultCollapseRuntimePanel(
      typeof window !== 'undefined' ? window.innerWidth : Number.POSITIVE_INFINITY,
      audience,
    ));
  }, [audience]);
  React.useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return undefined;
    const query = window.matchMedia('(max-width: 960px)');
    const handleChange = (event: MediaQueryListEvent | MediaQueryList) => {
      if (event.matches) setCollapsed(true);
    };
    handleChange(query);
    query.addEventListener('change', handleChange);
    return () => query.removeEventListener('change', handleChange);
  }, []);
  return [collapsed, setCollapsed];
}

export function findSelectedThreadItem(
  messages: readonly AgentChatMessage[],
  selectedId: string | null,
): ThreadItem | null {
  if (!selectedId) return null;
  return messages.find((message) => message.threadItem?.id === selectedId)?.threadItem || null;
}

export function useThreadItemRuntimeController(
  sessionId: string | null,
  messages: readonly AgentChatMessage[],
) {
  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const [inspectorRevealRequest, setInspectorRevealRequest] = React.useState<RuntimeInspectorRevealRequest | null>(null);
  React.useEffect(() => {
    setSelectedId(null);
    setInspectorRevealRequest(null);
  }, [sessionId]);
  const selectedItem = React.useMemo(
    () => findSelectedThreadItem(messages, selectedId),
    [messages, selectedId],
  );
  const selectItem = React.useCallback((item: ThreadItem) => {
    setSelectedId(item.id);
    // Disclosure is anchored to this explicit selection action, not to the
    // selected object's identity: re-selecting the same item reveals again,
    // while a transcript rebuild that recreates the item never does.
    if (shouldRevealRuntimeInspectorForSelection(item)) {
      setInspectorRevealRequest({ itemId: item.id });
    }
  }, []);
  const clearSelection = React.useCallback(() => setSelectedId(null), []);

  return {
    selectedId,
    selectedItem,
    selectItem,
    clearSelection,
    inspectorRevealRequest,
  };
}
