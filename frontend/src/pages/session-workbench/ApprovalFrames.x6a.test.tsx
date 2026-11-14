// @vitest-environment jsdom

import { readFileSync } from 'node:fs';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import SessionPermissionActions from '../agent-detail/SessionPermissionActions';
import { projectSessionEventStoreToMessages } from '../agent-detail/sessionEventConsumer';
import { createSessionEventStore, reduceSessionEvent, type SessionEventV2 } from './sessionEventStore';
import { normalizeThreadItemPayload, threadItemToAgentChatMessage } from './threadItemReducer';

type Frames = { tool_name: string; allow_session: boolean; legacy: unknown; canonical: SessionEventV2 };
// The PostgreSQL regression supplies both real server producers; no hand-authored
// flag can make a frontend-only fixture pass while the legacy producer still omits it.
const fixture = process.env.HC_X6A_PERMISSION_FRAMES;
const frames: Frames[] = fixture ? JSON.parse(readFileSync(fixture, 'utf8')) : [{} as Frames];

afterEach(cleanup);

it.skipIf(!fixture).each(frames)(
  'keeps $tool_name choices stable from the legacy frame through the canonical frame',
  (frame) => {
    for (const catalog of [zh, en]) {
      const t = (key: string, fallback: string) => translateFromCatalog(catalog, key, fallback);
      const legacy = threadItemToAgentChatMessage(normalizeThreadItemPayload(frame.legacy)!);
      const canonical = projectSessionEventStoreToMessages(
        reduceSessionEvent(createSessionEventStore(frame.canonical.sequence - 1), frame.canonical),
      )[0];
      const resolve = vi.fn();
      // Approval cards own their details disclosure; compare their actual controls
      // rather than the standalone row's separate argument preview.
      const { container, rerender, unmount } = render(
        <SessionPermissionActions
          showArgumentDetails={false}
          permissionRequest={legacy.sessionPermissionRequest!}
          onResolveSessionPermission={resolve}
          t={t}
        />,
      );
      const before = screen.getAllByRole('button');
      const labels = before.map((button) => button.textContent);
      const copy = container.textContent;
      rerender(
        <SessionPermissionActions
          showArgumentDetails={false}
          permissionRequest={canonical.sessionPermissionRequest!}
          onResolveSessionPermission={resolve}
          t={t}
        />,
      );
      const after = screen.getAllByRole('button');
      expect(after.map((button) => button.textContent)).toEqual(labels);
      after.forEach((button, index) => expect(button).toBe(before[index]));
      expect(container.textContent).toBe(copy);
      expect(labels).toEqual([
        t('agent.chat.permission.allowOnce', 'Allow once'),
        ...(frame.allow_session ? [t('agent.chat.permission.allowSession', 'Allow for this session')] : []),
        t('agent.chat.permission.deny', 'Deny'),
      ]);
      unmount();
    }
  },
);
