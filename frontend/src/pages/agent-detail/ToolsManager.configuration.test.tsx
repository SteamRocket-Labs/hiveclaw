/** @vitest-environment jsdom */
import React from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import ToolsManager from './ToolsManager';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string, value: unknown) => typeof value === 'string' ? value : key }) }));
const api = vi.hoisted(() => ({ listWithConfig: vi.fn(), updateToolConfig: vi.fn(),
  getAgentExtensions: vi.fn(), getAgentMcpServerTools: vi.fn() }));
vi.mock('../../api/domains/tools', () => ({ toolsApi: api }));
vi.mock('../../api/domains/extensions', () => ({ extensionsApi: api }));
const tool = { id: 'tool', tool_id: 'tool', name: 'lookup', display_name: 'Lookup', type: 'mcp', category: 'mcp',
  mcp_server_name: 'Shared connector', mcp_tool_name: 'lookup', enabled: true, config: {},
  global_config: { endpoint: 'company-value', api_key: '__HIVE_SECRET_SET__' }, agent_config: { timeout: 3 },
  config_schema: { fields: [{ key: 'endpoint', label: 'Endpoint', type: 'text' },
    { key: 'api_key', label: 'Key', type: 'password' }, { key: 'timeout', label: 'Timeout', type: 'number' }] } };

afterEach(() => { cleanup(); vi.clearAllMocks(); });

async function openEditor() {
  fireEvent.click(await screen.findByRole('button', { name: /Advanced tool controls/ }));
  fireEvent.click(await screen.findByRole('button', { name: 'Configure' }));
}

function setup() {
  api.listWithConfig.mockResolvedValue([tool]);
  api.getAgentExtensions.mockResolvedValue({ mcp_servers: [{ id: 'server', name: 'Shared connector', status: 'available',
    enabled: true, tool_count: 1 }], plugins: [], external_activations: [] });
  api.getAgentMcpServerTools.mockResolvedValue([{ tool_name: 'lookup', mode: 'inherit', effective_mode: 'allow' }]);
}

it('keeps company defaults inherited and displays the actual masked server readback', async () => {
  setup();
  const saved = { ...tool, config: { ...tool.global_config, timeout: 7 }, agent_config: { timeout: 7 } };
  api.updateToolConfig.mockResolvedValue({ ok: true, tool: saved });
  render(<ToolsManager agentId="employee" canManage />);
  await openEditor();
  fireEvent.change(screen.getByDisplayValue('3'), { target: { value: '7' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(api.updateToolConfig).toHaveBeenCalledWith('employee', 'tool', { timeout: 7 }));
  await waitFor(() => expect(screen.queryByDisplayValue('7')).toBeNull());
  fireEvent.click(await screen.findByRole('button', { name: 'Configure' }));
  expect((screen.getByDisplayValue('company-value') as HTMLInputElement).value).toBe('company-value');
  expect((screen.getByDisplayValue('7') as HTMLInputElement).value).toBe('7');
  expect(api.listWithConfig).toHaveBeenCalledTimes(1);
});

it.each(['second', 'first'])('does not apply a late save to a new settings view for %s', async (destination) => {
  setup();
  let settle!: (value: unknown) => void;
  api.updateToolConfig.mockImplementation(() => new Promise((resolve) => { settle = resolve; }));
  const view = render(<ToolsManager agentId="first" canManage />);
  await openEditor();
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(api.updateToolConfig).toHaveBeenCalled());
  view.rerender(<ToolsManager agentId="second" canManage />);
  await waitFor(() => expect(api.listWithConfig).toHaveBeenCalledWith('second'));
  await waitFor(() => expect(screen.queryByDisplayValue('3')).toBeNull());
  if (destination === 'first') {
    view.rerender(<ToolsManager agentId="first" canManage />);
    await waitFor(() => expect(api.listWithConfig).toHaveBeenCalledTimes(3));
  }
  await openEditor();
  fireEvent.change(screen.getByDisplayValue('3'), { target: { value: '9' } });
  await act(async () => { settle({ ok: true, tool: { ...tool, agent_config: { timeout: 111 } } }); });
  await waitFor(() => expect((screen.getByDisplayValue('9') as HTMLInputElement).value).toBe('9'));
  expect(screen.queryByDisplayValue('111')).toBeNull();
});
