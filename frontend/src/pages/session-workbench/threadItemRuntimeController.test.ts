// @vitest-environment jsdom

import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import type { AgentChatMessage } from '../agent-detail/chatRuntime';
import type { ThreadItem } from '../../api/domains/threadItems.generated';
import {
  findSelectedThreadItem,
  shouldDefaultCollapseRuntimePanel,
  shouldRevealRuntimeInspectorForSelection,
  useOperatorInspectorReveal,
  useResponsiveRuntimePanel,
  useThreadItemRuntimeController,
  type RuntimePanelAudience,
} from './threadItemRuntimeController';

describe('ThreadItem runtime controller', () => {
  it('tracks the newest item object by stable id after in-place status replacement', () => {
    const item = {
      schema: 'hive.thread_item.v1',
      schema_version: 1,
      id: 'item-1',
      sequence: 1,
      item_type: 'event',
      item_status: 'succeeded',
      actor_type: 'system',
      event_type: 'hook_progress',
      type: 'hook_progress',
      role: 'system',
      visibility_scope: 'direct_user',
      listed_surface: 'chat',
      content: 'done',
      item_data: { event_type: 'hook_progress' },
    } as ThreadItem;
    const messages = [{ role: 'event', content: 'done', threadItem: item }] as AgentChatMessage[];

    expect(findSelectedThreadItem(messages, 'item-1')).toBe(item);
    expect(findSelectedThreadItem(messages, 'missing')).toBeNull();
    expect(findSelectedThreadItem(messages, null)).toBeNull();
  });

  it('keeps the ordinary-user inspector collapsed by default at every workbench width', () => {
    expect(shouldDefaultCollapseRuntimePanel(1440, 'user')).toBe(true);
    expect(shouldDefaultCollapseRuntimePanel(961, 'user')).toBe(true);
    expect(shouldDefaultCollapseRuntimePanel(960, 'user')).toBe(true);
    expect(shouldDefaultCollapseRuntimePanel(740, 'user')).toBe(true);
    expect(shouldDefaultCollapseRuntimePanel(1440)).toBe(true);
  });

  it('keeps the operator evidence rail open by default only on wide workbenches', () => {
    expect(shouldDefaultCollapseRuntimePanel(1440, 'operator')).toBe(false);
    expect(shouldDefaultCollapseRuntimePanel(961, 'operator')).toBe(false);
    expect(shouldDefaultCollapseRuntimePanel(960, 'operator')).toBe(true);
    expect(shouldDefaultCollapseRuntimePanel(740, 'operator')).toBe(true);
  });

  it('reveals the runtime inspector only for an explicit operator technical-detail selection', () => {
    const operatorItem = {
      id: 'item-operator',
      audience: 'operator',
      operator_details: { item_data: {} },
    } as unknown as ThreadItem;
    const userItem = {
      id: 'item-user',
      audience: 'user',
      operator_details: null,
    } as unknown as ThreadItem;
    const operatorWithoutDetails = {
      id: 'item-operator-empty',
      audience: 'operator',
      operator_details: null,
    } as unknown as ThreadItem;

    expect(shouldRevealRuntimeInspectorForSelection(operatorItem)).toBe(true);
    expect(shouldRevealRuntimeInspectorForSelection(userItem)).toBe(false);
    expect(shouldRevealRuntimeInspectorForSelection(operatorWithoutDetails)).toBe(false);
    expect(shouldRevealRuntimeInspectorForSelection(null)).toBe(false);
  });
});

const originalInnerWidth = window.innerWidth;

function setViewportWidth(width: number) {
  Object.defineProperty(window, 'innerWidth', { value: width, configurable: true, writable: true });
}

afterEach(() => {
  setViewportWidth(originalInnerWidth);
  cleanup();
});

const OPERATOR_DETAIL_ITEM = {
  id: 'item-operator-detail',
  audience: 'operator',
  operator_details: { item_data: {} },
} as unknown as ThreadItem;
const OPERATOR_DETAIL_MESSAGE = {
  role: 'event',
  content: 'operator evidence',
  threadItem: OPERATOR_DETAIL_ITEM,
} as AgentChatMessage;
const USER_NOTE_ITEM = {
  id: 'item-user-note',
  audience: 'user',
  operator_details: null,
} as unknown as ThreadItem;

function useComposedRuntimePanel(sessionId: string, messages: readonly AgentChatMessage[]) {
  const controller = useThreadItemRuntimeController(sessionId, messages);
  const [collapsed, setCollapsed] = useResponsiveRuntimePanel('operator');
  useOperatorInspectorReveal(controller.inspectorRevealRequest, setCollapsed);
  return { ...controller, collapsed, setCollapsed };
}

describe('useResponsiveRuntimePanel audience resolution', () => {
  function renderPanelHook(initialAudience: RuntimePanelAudience) {
    return renderHook(
      ({ audience }: { audience: RuntimePanelAudience }) => useResponsiveRuntimePanel(audience),
      { initialProps: { audience: initialAudience } },
    );
  }

  it('applies the wide operator default once operator authority resolves after mount', () => {
    setViewportWidth(1440);
    const hook = renderPanelHook('user');
    expect(hook.result.current[0]).toBe(true);

    hook.rerender({ audience: 'operator' });

    expect(hook.result.current[0]).toBe(false);
  });

  it('re-applies the task-first default when the audience migrates back to an ordinary session', () => {
    setViewportWidth(1440);
    const hook = renderPanelHook('operator');
    expect(hook.result.current[0]).toBe(false);

    hook.rerender({ audience: 'user' });

    expect(hook.result.current[0]).toBe(true);
  });

  it('keeps a manual panel choice on stable-audience rerenders and background updates', () => {
    setViewportWidth(1440);
    const hook = renderPanelHook('operator');
    expect(hook.result.current[0]).toBe(false);
    act(() => hook.result.current[1](true));
    expect(hook.result.current[0]).toBe(true);

    hook.rerender({ audience: 'operator' });

    expect(hook.result.current[0]).toBe(true);
  });

  it('keeps the narrow-viewport collapse policy when the audience migrates', () => {
    setViewportWidth(900);
    const hook = renderPanelHook('user');
    expect(hook.result.current[0]).toBe(true);

    hook.rerender({ audience: 'operator' });

    expect(hook.result.current[0]).toBe(true);
  });

  it('collapses an open operator rail when the viewport crosses into the narrow band', () => {
    setViewportWidth(1440);
    const listeners = new Set<(event: { matches: boolean }) => void>();
    const matchMediaStub = (query: string) => ({
      matches: false,
      media: query,
      addEventListener: (_event: string, listener: (event: { matches: boolean }) => void) => {
        listeners.add(listener);
      },
      removeEventListener: (_event: string, listener: (event: { matches: boolean }) => void) => {
        listeners.delete(listener);
      },
    });
    Object.defineProperty(window, 'matchMedia', { value: matchMediaStub, configurable: true });
    try {
      const hook = renderHook(() => useResponsiveRuntimePanel('operator'));
      expect(hook.result.current[0]).toBe(false);

      act(() => {
        for (const listener of listeners) listener({ matches: true });
      });

      expect(hook.result.current[0]).toBe(true);
    } finally {
      delete (window as { matchMedia?: unknown }).matchMedia;
    }
  });
});

describe('explicit selection inspector disclosure', () => {
  it('reveals the inspector on every explicit operator selection, including the same item twice', () => {
    setViewportWidth(1440);
    const hook = renderHook(() => useComposedRuntimePanel('session-1', [OPERATOR_DETAIL_MESSAGE]));
    act(() => hook.result.current.setCollapsed(true));
    act(() => hook.result.current.selectItem(OPERATOR_DETAIL_ITEM));
    expect(hook.result.current.collapsed).toBe(false);

    act(() => hook.result.current.setCollapsed(true));
    expect(hook.result.current.collapsed).toBe(true);
    act(() => hook.result.current.selectItem(OPERATOR_DETAIL_ITEM));

    expect(hook.result.current.collapsed).toBe(false);
    expect(hook.result.current.selectedId).toBe(OPERATOR_DETAIL_ITEM.id);
  });

  it('does not force the rail open when a transcript rebuild recreates the selected item', () => {
    setViewportWidth(1440);
    const hook = renderHook(
      ({ messages }: { messages: AgentChatMessage[] }) => useComposedRuntimePanel('session-1', messages),
      { initialProps: { messages: [OPERATOR_DETAIL_MESSAGE] } },
    );
    act(() => hook.result.current.selectItem(OPERATOR_DETAIL_ITEM));
    expect(hook.result.current.collapsed).toBe(false);
    act(() => hook.result.current.setCollapsed(true));

    const rebuiltItem = { ...OPERATOR_DETAIL_ITEM } as ThreadItem;
    hook.rerender({ messages: [{ ...OPERATOR_DETAIL_MESSAGE, threadItem: rebuiltItem }] });

    expect(hook.result.current.collapsed).toBe(true);
    expect(hook.result.current.selectedItem).toBe(rebuiltItem);
  });

  it('ignores explicit selections that carry no operator details', () => {
    setViewportWidth(1440);
    const hook = renderHook(() => useComposedRuntimePanel('session-1', [OPERATOR_DETAIL_MESSAGE]));
    act(() => hook.result.current.setCollapsed(true));

    act(() => hook.result.current.selectItem(USER_NOTE_ITEM));

    expect(hook.result.current.collapsed).toBe(true);
    expect(hook.result.current.selectedId).toBe(USER_NOTE_ITEM.id);
  });

  it('clears the selection when the session changes', () => {
    setViewportWidth(1440);
    const hook = renderHook(
      ({ sessionId }: { sessionId: string }) => useComposedRuntimePanel(sessionId, [OPERATOR_DETAIL_MESSAGE]),
      { initialProps: { sessionId: 'session-1' } },
    );
    act(() => hook.result.current.selectItem(OPERATOR_DETAIL_ITEM));
    expect(hook.result.current.selectedId).toBe(OPERATOR_DETAIL_ITEM.id);

    hook.rerender({ sessionId: 'session-2' });

    expect(hook.result.current.selectedId).toBeNull();
    expect(hook.result.current.selectedItem).toBeNull();
  });
});
