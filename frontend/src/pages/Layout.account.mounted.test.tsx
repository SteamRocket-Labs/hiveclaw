// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { AccountSettingsModal } from './Layout';
import { useAuthStore } from '../stores';
import type { User } from '../types';

const calls = vi.hoisted(() => ({ start: vi.fn(), poll: vi.fn(), me: vi.fn() }));
vi.mock('../api/domains/auth', () => ({ authApi: { feishuBindInit: calls.start, feishuSsoPoll: calls.poll, getMe: calls.me }, feishuConflictMessage: () => null }));
vi.mock('../components/TenantFeishuSettings', () => ({ TenantFeishuIdentity: () => null }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string, fallback?: unknown) => typeof fallback === 'string' ? fallback : key }) }));
afterEach(() => { cleanup(); useAuthStore.getState().logout(); vi.restoreAllMocks(); vi.resetAllMocks(); });

it('does not open a provider popup or resume polling after the account dialog closes during initialization', async () => {
  const user = { id: 'owner', tenant_id: 'company', role: 'org_admin', username: 'Owner' } as User;
  useAuthStore.getState().setAuth(user, 'synthetic-account-token');
  let finish!: (value: unknown) => void;
  calls.start.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  const popup = vi.spyOn(window, 'open').mockReturnValue(null);
  const view = render(<AccountSettingsModal user={user} onClose={() => {}} />);
  fireEvent.click(screen.getByRole('button', { name: 'Link Feishu Account' }));
  view.unmount();
  await act(async () => { finish({ session_id: 'pending-account', authorize_url: 'https://example.test/authorize' }); });
  expect(popup).not.toHaveBeenCalled();
  expect(calls.poll).not.toHaveBeenCalled();
  expect(calls.me).not.toHaveBeenCalled();
});
