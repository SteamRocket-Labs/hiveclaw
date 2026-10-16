/** @vitest-environment jsdom */
import React from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import ChannelConfig from './ChannelConfig';

vi.mock('react-i18next', () => {
  const t = (key: string, fallback: unknown) => typeof fallback === 'string' ? fallback : key;
  return { useTranslation: () => ({ t }) };
});
const api = vi.hoisted(() => ({ get: vi.fn(), getChannelConfig: vi.fn(), getChannelWebhook: vi.fn(),
  createChannelConfig: vi.fn(), getAgentFeishuRuntimeStatus: vi.fn() }));
vi.mock('../api/domains/channels', () => ({ channelApi: api }));
vi.mock('../api/domains/tools', () => ({ toolsApi: api }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

it('loads only the requested channel and keeps a failed draft until the existing API confirms its save', async () => {
  api.getChannelConfig.mockResolvedValue(null);
  api.getChannelWebhook.mockResolvedValue(null);
  let fail!: (reason: Error) => void;
  let finish!: (value: unknown) => void;
  api.createChannelConfig.mockImplementationOnce(() => new Promise((_resolve, reject) => { fail = reject; }))
    .mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  const configured = vi.fn(), busy = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const view = render(<QueryClientProvider client={client}><ChannelConfig mode="edit" agentId="target"
    channelType="slack" onConfigured={() => { configured(); view.unmount(); }} onBusyChange={busy} /></QueryClientProvider>);
  await waitFor(() => expect(api.getChannelConfig).toHaveBeenCalledWith('target', 'slack-channel'));
  expect(api.getChannelConfig.mock.calls.every(([, slug]) => slug === 'slack-channel')).toBe(true);
  expect(api.get).not.toHaveBeenCalled();
  expect(api.getAgentFeishuRuntimeStatus).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText(/Bot Token/), { target: { value: 'synthetic-bot-token' } });
  fireEvent.change(screen.getByLabelText(/Signing Secret/), { target: { value: 'synthetic-signing-secret' } });
  fireEvent.click(screen.getByRole('button', { name: 'agent.settings.channel.saveChannel' }));
  await waitFor(() => expect(busy).toHaveBeenCalledWith(true));
  await act(async () => fail(new Error('Synthetic save failure')));
  await screen.findByText('Synthetic save failure');
  expect(configured).not.toHaveBeenCalled();
  expect((screen.getByLabelText(/Bot Token/) as HTMLInputElement).value).toBe('synthetic-bot-token');
  fireEvent.click(screen.getByRole('button', { name: 'agent.settings.channel.saveChannel' }));
  await waitFor(() => expect(api.createChannelConfig).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(busy).toHaveBeenLastCalledWith(true));
  await act(async () => finish({ is_configured: true }));
  await waitFor(() => expect(configured).toHaveBeenCalledTimes(1));
  expect(api.createChannelConfig).toHaveBeenLastCalledWith('target', 'slack-channel', {
    bot_token: 'synthetic-bot-token', signing_secret: 'synthetic-signing-secret' });
  expect(busy).toHaveBeenCalledWith(true);
  expect(busy).toHaveBeenLastCalledWith(false);
  client.clear();
});
