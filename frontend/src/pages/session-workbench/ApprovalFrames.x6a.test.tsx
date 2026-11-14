// @vitest-environment jsdom

import { readFileSync } from 'node:fs';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import SessionPermissionActions from '../agent-detail/SessionPermissionActions';
import { compatibilityProjectionEvent, projectSessionEventStoreToMessages } from '../agent-detail/sessionEventConsumer';
import type { AgentChatMessage } from '../agent-detail/chatRuntime';
import { ThreadItemRenderer } from './ThreadItemRenderer';
import {
  createSessionEventStore,
  reduceSessionEvent,
  type SessionCompatibilityEvent,
  type SessionEventV2,
} from './sessionEventStore';
import { normalizeThreadItemPayload, threadItemToAgentChatMessage } from './threadItemReducer';

const locale = vi.hoisted(() => ({ value: 'zh' }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string, values?: Record<string, unknown>) =>
      translateFromCatalog(locale.value === 'zh' ? zh : en, key, fallback, values),
    i18n: { language: locale.value },
  }),
}));

type Frames = {
  tool_name: string;
  allow_session: boolean;
  legacy: SessionCompatibilityEvent;
  canonical: SessionEventV2;
  reload: SessionEventV2;
};
// The backend integration supplies real committed frames and runs this file.
// Keep the plain frontend suite usable: without that fixture this file is skipped.
const fixture = process.env.HC_X6A_PERMISSION_FRAMES;
const frames: Frames[] = fixture ? JSON.parse(readFileSync(fixture, 'utf8')) : [{} as Frames];
afterEach(cleanup);

function canonicalMessage(event: SessionEventV2): AgentChatMessage {
  return projectSessionEventStoreToMessages(reduceSessionEvent(createSessionEventStore(event.sequence - 1), event))[0];
}

function cardItem(msg: AgentChatMessage) {
  // This is the same compatibility/canonical seam as AgentChatSection.renderEventMessage.
  return (
    msg.threadItem ||
    normalizeThreadItemPayload({
      id: msg.transcriptEventId || msg.id,
      eventType: msg.eventType,
      content: msg.content,
      status: msg.eventStatus,
      tool_name: msg.toolName,
      permission_request: msg.sessionPermissionRequest,
      created_at: msg.timestamp,
    })!
  );
}

it.skipIf(!fixture).each(frames.flatMap((frame) => ['zh', 'en'].map((language) => ({ ...frame, language }))))(
  'keeps the whole $tool_name card identical across compatibility, canonical and reload in $language',
  (frame) => {
    locale.value = frame.language;
    const catalog = frame.language === 'zh' ? zh : en;
    const t = (key: string, fallback: string) => translateFromCatalog(catalog, key, fallback);
    const legacy = threadItemToAgentChatMessage(
      normalizeThreadItemPayload(compatibilityProjectionEvent(frame.legacy))!,
    );
    const resolve = vi.fn();
    const card = (message: AgentChatMessage) => (
      <ThreadItemRenderer
        item={cardItem(message)}
        approvalArguments={message.sessionPermissionRequest!.arguments}
        approvalActions={
          <SessionPermissionActions
            showArgumentDetails={false}
            permissionRequest={message.sessionPermissionRequest!}
            onResolveSessionPermission={resolve}
            t={t}
          />
        }
      />
    );
    const { container, rerender } = render(card(legacy));
    const snapshot = () => ({
      text: container.textContent,
      rows: Array.from(container.querySelectorAll('dt, dd')).map((row) => row.textContent),
      buttons: screen.getAllByRole('button').map((button) => button.textContent),
    });
    const first = snapshot();
    const nodes = screen.getAllByRole('button');
    for (const event of [frame.canonical, frame.reload]) {
      rerender(card(canonicalMessage(event)));
      expect(snapshot()).toEqual(first);
      screen.getAllByRole('button').forEach((button, index) => expect(button).toBe(nodes[index]));
    }
    expect(first.buttons).toEqual([
      t('agent.chat.permission.fullArguments', 'See exactly what will run'),
      t('agent.chat.permission.allowOnce', 'Allow once'),
      ...(frame.allow_session ? [t('agent.chat.permission.allowSession', 'Allow for this session')] : []),
      t('agent.chat.permission.deny', 'Deny'),
    ]);
    if (frame.tool_name === 'delete_file') {
      expect(first.text).toContain(
        t('agent.chat.permission.deleteOnceOnly', 'Delete operations can only be allowed once.'),
      );
    }
    if (frame.tool_name === 'plaza_create_post') {
      expect(first.rows).toContain('Team update');
    } else {
      expect(first.rows).toContain(t('sessionWorkbench.threadItem.value.destructive', 'Irreversible action'));
    }
  },
);
