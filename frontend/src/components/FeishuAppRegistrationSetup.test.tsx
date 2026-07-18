import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import {
  default as FeishuAppRegistrationSetup,
  isFeishuRegistrationCancellable,
  phaseFromFeishuRegistrationStatus,
} from './FeishuAppRegistrationSetup';
import en from '../i18n/en.json';
import zh from '../i18n/zh.json';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string) => fallback ?? _key,
  }),
}));

vi.mock('@tanstack/react-query', () => ({
  useQuery: ({ queryKey }: { queryKey: unknown[] }) => {
    if (queryKey[0] === 'feishu-app-registration-active') {
      return {
        data: {
          session_id: 'registration-1',
          status: 'scanning',
          platform_region: 'lark_global',
          verification_url: 'https://accounts.larksuite.com/page/launcher?ticket=test',
          connected: false,
          cancellable: true,
          created_at: '2026-07-18T00:00:00+00:00',
          updated_at: '2026-07-18T00:00:01+00:00',
        },
        isLoading: false,
        error: null,
      };
    }
    return { data: null, isLoading: false, error: null };
  },
  useMutation: () => ({
    mutate: vi.fn(),
    mutateAsync: vi.fn(),
    isPending: false,
    error: null,
  }),
  useQueryClient: () => ({
    setQueryData: vi.fn(),
  }),
}));

describe('FeishuAppRegistrationSetup', () => {
  it('keeps app registration separate from the real WebSocket connection state', () => {
    expect(phaseFromFeishuRegistrationStatus('scanning')).toBe('scanning');
    expect(phaseFromFeishuRegistrationStatus('polling')).toBe('scanning');
    expect(phaseFromFeishuRegistrationStatus('credentials_received')).toBe('connecting');
    expect(phaseFromFeishuRegistrationStatus('connecting')).toBe('connecting');
    expect(phaseFromFeishuRegistrationStatus('connected')).toBe('done');
  });

  it('allows cancellation only before credential persistence is fenced', () => {
    expect(isFeishuRegistrationCancellable('initializing')).toBe(true);
    expect(isFeishuRegistrationCancellable('scanning')).toBe(true);
    expect(isFeishuRegistrationCancellable('polling')).toBe(true);
    expect(isFeishuRegistrationCancellable('credentials_received')).toBe(false);
    expect(isFeishuRegistrationCancellable('connecting')).toBe(false);
    expect(isFeishuRegistrationCancellable('connected')).toBe(false);
  });

  it('has explicit QR setup copy for both Feishu and Lark', () => {
    const zhMessages = zh as Record<string, any>;
    const enMessages = en as Record<string, any>;

    expect(zhMessages.agent.settings.channel.registration.feishuScanPrompt).toContain('飞书');
    expect(zhMessages.agent.settings.channel.registration.larkScanPrompt).toContain('Lark');
    expect(enMessages.agent.settings.channel.registration.feishuScanPrompt).toContain('Feishu');
    expect(enMessages.agent.settings.channel.registration.larkScanPrompt).toContain('Lark');
  });

  it('renders only the authenticated official registration URL as the QR target', () => {
    const markup = renderToStaticMarkup(
      <FeishuAppRegistrationSetup agentId="agent-1" platformRegion="feishu_cn" />,
    );

    expect(markup).toContain('Scan with Lark to create or bind the app');
    expect(markup).toContain('https://accounts.larksuite.com/page/launcher?ticket=test');
    expect(markup).toContain('<svg');
  });
});
