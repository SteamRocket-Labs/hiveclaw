/** Actual employee A2A management consumes owned JWT HTTP and scoped credentials. */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { I18nextProvider } from 'react-i18next';
import i18next from 'i18next';
import { expect, it } from 'vitest';
import AgentA2ASection from '../src/pages/agent-detail/AgentA2ASection';
import en from '../src/i18n/en.json';
import { useAuthStore } from '../src/stores';
import type { User } from '../src/types';

it('issues from the real A2A tab, uses a distinct machine credential, and revokes it', async () => {
  const base = process.env.HIVE_G90_NATIVE_URL || '';
  expect(base.startsWith('http://127.0.0.1:')).toBe(true);
  const original = globalThis.fetch;
  globalThis.fetch = (url, options) => {
    if (typeof url !== 'string' || !url.startsWith('/api/')) throw new Error('Only owned loopback HTTP is permitted');
    return original(new URL(url, base), options);
  };
  const language = i18next.createInstance();
  await language.init({ lng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
  const userToken = process.env.HIVE_M3H_TOKEN || '';
  const agentId = process.env.HIVE_G90_AGENT_ID || '';
  useAuthStore.getState().setAuth({ id: process.env.HIVE_G90_USER_ID, tenant_id: process.env.HIVE_G90_TENANT_ID,
    role: 'org_admin', username: 'Credential issuer' } as User, userToken);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  try {
    render(<I18nextProvider i18n={language}><QueryClientProvider client={client}>
      <AgentA2ASection agentId={agentId} canManage />
    </QueryClientProvider></I18nextProvider>);
    fireEvent.change(await screen.findByLabelText('Integration name'), { target: { value: 'Native scoped partner' } });
    fireEvent.click(screen.getByLabelText('Run tasks'));
    fireEvent.click(screen.getByLabelText('Read task status and files'));
    fireEvent.click(screen.getByLabelText('Cancel queued or running tasks'));
    fireEvent.click(screen.getByRole('button', { name: 'Issue credential' }));
    const input = await screen.findByLabelText('New machine secret') as HTMLInputElement;
    const machineToken = input.value;
    expect(machineToken.startsWith('hmi_')).toBe(true);
    expect(machineToken).not.toBe(userToken);
    expect(localStorage.getItem('token')).toBe(userToken);
    expect(JSON.stringify(client.getQueryCache().getAll().map((query) => query.state.data))).not.toContain(machineToken);
    const description = await fetch(`/api/agents/${agentId}/machine-description`, { headers: { Authorization: `Bearer ${machineToken}` } });
    expect(description.status).toBe(200);
    const details = await description.json();
    expect(details.agent.id).toBe(agentId);
    expect(details.interoperability.a2a_tasks.methods).toEqual({
      SendMessage: 'agent.execute', GetTask: 'task.read', CancelTask: 'task.cancel',
    });
    const cardPath = new URL(details.interoperability.a2a_tasks.agent_card_url).pathname;
    expect(cardPath).toBe(`/api/agents/${agentId}/a2a/.well-known/agent-card.json`);
    expect(screen.getByText(cardPath)).toBeTruthy();
    const discovery = await fetch(cardPath, { headers: { Authorization: `Bearer ${machineToken}` } });
    expect(discovery.status).toBe(200);
    const card = await discovery.json();
    expect(card.supportedInterfaces[0].protocolBinding).toBe('JSONRPC');
    expect(card.supportedInterfaces[0].protocolVersion).toBe('1.0');
    const taskPath = new URL(card.supportedInterfaces[0].url).pathname;
    expect(taskPath).toBe(`/api/agents/${agentId}/a2a/rpc`);
    const headers = { Authorization: `Bearer ${machineToken}`, 'A2A-Version': '1.0', 'Content-Type': 'application/json' };
    const submit = await fetch(taskPath, { method: 'POST', headers, body: JSON.stringify({
      jsonrpc: '2.0', id: 'ui-send', method: 'SendMessage', params: {
        message: { messageId: crypto.randomUUID(), role: 'ROLE_USER', parts: [{ text: 'Prepare the partner report.' }] },
        configuration: { returnImmediately: true },
      },
    }) });
    expect(submit.status).toBe(200);
    const task = (await submit.json()).result.task;
    const read = await fetch(taskPath, { method: 'POST', headers, body: JSON.stringify({
      jsonrpc: '2.0', id: 'ui-read', method: 'GetTask', params: { id: task.id },
    }) });
    expect((await read.json()).result.id).toBe(task.id);
    const cancel = await fetch(taskPath, { method: 'POST', headers, body: JSON.stringify({
      jsonrpc: '2.0', id: 'ui-cancel', method: 'CancelTask', params: { id: task.id },
    }) });
    expect((await cancel.json()).result.metadata.hive.cancellationStatus).toBe('applying');
    fireEvent.click(screen.getByRole('button', { name: 'Revoke access' }));
    await screen.findByText('Native scoped partner · Revoked');
    await waitFor(() => expect(screen.queryByLabelText('New machine secret')).toBeNull());
    const refused = await fetch(`/api/agents/${agentId}/machine-description`, { headers: { Authorization: `Bearer ${machineToken}` } });
    expect(refused.status).toBe(401);
    expect(localStorage.getItem('token')).toBe(userToken);
  } finally {
    cleanup();client.clear();useAuthStore.getState().logout();globalThis.fetch = original;
  }
});
