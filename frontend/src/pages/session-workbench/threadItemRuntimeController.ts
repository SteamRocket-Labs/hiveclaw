import React from 'react';

import type { ThreadItem } from '../../api/domains/threadItems.generated';
import type { AgentChatMessage } from '../agent-detail/chatRuntime';

export function shouldDefaultCollapseRuntimePanel(viewportWidth: number): boolean {
  return viewportWidth <= 960;
}

export function useResponsiveRuntimePanel(): [boolean, React.Dispatch<React.SetStateAction<boolean>>] {
  const [collapsed, setCollapsed] = React.useState(() => (
    typeof window !== 'undefined' ? shouldDefaultCollapseRuntimePanel(window.innerWidth) : false
  ));
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
  React.useEffect(() => setSelectedId(null), [sessionId]);
  const selectedItem = React.useMemo(
    () => findSelectedThreadItem(messages, selectedId),
    [messages, selectedId],
  );
  const selectItem = React.useCallback((item: ThreadItem) => setSelectedId(item.id), []);
  const clearSelection = React.useCallback(() => setSelectedId(null), []);

  return {
    selectedId,
    selectedItem,
    selectItem,
    clearSelection,
  };
}
