import { describe, expect, it } from 'vitest';

import type { AgentChatMessage } from '../agent-detail/chatRuntime';
import type { ThreadItem } from '../../api/domains/threadItems.generated';
import { findSelectedThreadItem, shouldDefaultCollapseRuntimePanel } from './threadItemRuntimeController';

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

  it('defaults the inspector rail to collapsed only at narrow workbench widths', () => {
    expect(shouldDefaultCollapseRuntimePanel(1440)).toBe(false);
    expect(shouldDefaultCollapseRuntimePanel(961)).toBe(false);
    expect(shouldDefaultCollapseRuntimePanel(960)).toBe(true);
    expect(shouldDefaultCollapseRuntimePanel(740)).toBe(true);
  });
});
