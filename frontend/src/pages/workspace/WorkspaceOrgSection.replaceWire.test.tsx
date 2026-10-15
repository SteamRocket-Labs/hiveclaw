// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createInstance, type i18n as I18nInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import WorkspaceOrgSection from './WorkspaceOrgSection';

const words = en.enterprise.org;
let i18n: I18nInstance;
const requests: Array<{ url: string; method: string; body: unknown }> = [];

const storedLink = {
  id: 'link-ada',
  org_member_id: 'member-ada',
  user_id: 'user-ada',
  provider_id: 'provider-home',
  subject_kind: 'provider_open_id',
  status: 'active',
  stored_status: 'active',
  version: 'version-1',
  source: 'manual',
  created_by_user_id: 'admin-1',
  valid: true,
  reason: 'active',
};

const replacedLink = {
  ...storedLink,
  id: 'link-replaced',
  user_id: 'user-bea',
  version: 'version-2',
};

let saved = false;

function directory() {
  const link = saved ? replacedLink : storedLink;
  return {
    members: [
      {
        org_member_id: 'member-ada',
        label: 'Ada Person',
        subject_kinds: ['provider_open_id', 'provider_union_id'],
        reason: 'active',
        valid: true,
        links: [link],
      },
    ],
    accounts: [
      { user_id: 'user-ada', label: 'Ada', is_active: true },
      { user_id: 'user-bea', label: 'Bea', is_active: true },
    ],
    providers: [
      { id: 'provider-home', provider_type: 'feishu', name: 'Home Feishu', is_active: true },
    ],
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

beforeAll(async () => {
  i18n = createInstance();
  await i18n.init({
    lng: 'en',
    resources: { en: { translation: en }, zh: { translation: zh } },
    interpolation: { escapeValue: false },
  });
});

beforeEach(() => {
  saved = false;
  requests.length = 0;
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method || 'GET';
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    requests.push({ url: String(url), method, body });
    if (String(url).includes('/identity-links') && method === 'POST') {
      saved = true;
      return jsonResponse({
        applied: true,
        code: 'active',
        reason: 'active',
        message: 'The link was saved.',
        retryable: false,
        link: replacedLink,
      });
    }
    if (String(url).includes('/identity-links')) return jsonResponse(directory());
    if (String(url).includes('/departments') || String(url).includes('/org/members')) return jsonResponse([]);
    if (String(url).includes('feishu-status')) return jsonResponse(null);
    return jsonResponse({ value: { app_id: 'cli_test' } });
  }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('stored identity replace wire', () => {
  it('sends the real adapter replace body, persists the result, and reloads it', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <I18nextProvider i18n={i18n}>
          <WorkspaceOrgSection selectedTenantId="tenant-1" />
        </I18nextProvider>
      </QueryClientProvider>,
    );
    await screen.findByRole('option', { name: /Ada Person/ });
    fireEvent.change(screen.getByLabelText(words.identityRepairPerson), { target: { value: 'member-ada' } });
    fireEvent.change(screen.getByLabelText(words.identityRepairAccount), { target: { value: 'user-bea' } });
    fireEvent.change(screen.getByLabelText(words.identityRepairProvider), { target: { value: 'provider-home' } });
    fireEvent.change(screen.getByLabelText(words.identityRepairKind), { target: { value: 'provider_open_id' } });
    fireEvent.click(screen.getByRole('button', { name: words.identityRepairConfirm }));
    expect(await screen.findByText(/link-replaced/)).toBeTruthy();
    const replace = requests.filter((item) => item.method === 'POST' && item.url.includes('/replace'));
    expect(replace).toHaveLength(1);
    expect(replace[0].body).toEqual({
      user_id: 'user-bea',
      provider_id: 'provider-home',
      subject_kind: 'provider_open_id',
      expected_version: 'version-1',
      expected_user_id: 'user-ada',
      expected_provider_id: 'provider-home',
      expected_subject_kind: 'provider_open_id',
      expected_status: 'active',
    });
    expect(replace[0].body).not.toHaveProperty('org_member_id');
    await waitFor(() => expect(requests.filter((item) => item.method === 'GET' && item.url.includes('/identity-links')).length).toBeGreaterThan(1));
    expect(await screen.findByRole('option', { name: /Ada Person · Active/ })).toBeTruthy();
  });
});
