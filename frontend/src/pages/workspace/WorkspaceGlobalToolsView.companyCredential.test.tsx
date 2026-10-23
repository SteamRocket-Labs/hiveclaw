import type { ReactElement, ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { toolsApi } from '../../api/domains/tools';
import { requestAppConfirm, showAppToast } from '../../components/AppDialogs';
import en from '../../i18n/en.json';
import WorkspaceGlobalToolsView from './WorkspaceGlobalToolsView';

const state = vi.hoisted(() => ({
  index: 0,
  editing: false,
  expanded: false,
  tool: { id: 'company-tool', tenant_id: 'company', name: 'mcp_ledger', display_name: 'Ledger',
    type: 'mcp', category: 'mcp', enabled: false, config: { api_key: '********' },
    mcp_server_name: 'Ledger', mcp_server_url: 'https://ledger.example/mcp', mcp_metadata_fingerprint: 'a'.repeat(64) },
}));

// Exercise the real React element callbacks in Node; no DOM, browser or mounted UI.
vi.mock('react', async (original) => {
  const actual = await original<typeof import('react')>();
  return { ...actual, useEffect: vi.fn(), useState: (initial: unknown) => {
    const index = state.index++;
    const value = index === 0 ? [state.tool] : index === 13 ? new Set(state.expanded ? ['Ledger'] : []) : state.editing && index === 9 ? state.tool.id :
      state.editing && index === 10 ? state.tool.config : initial;
    return [value, vi.fn((next: unknown) => {
      if (index === 13) {
        const updated = typeof next === 'function' ? next(value) : next;
        state.expanded = updated instanceof Set && updated.has('Ledger');
      }
    })];
  } };
});
vi.mock('../../api/domains/tools', () => ({ toolsApi: { listCatalog: vi.fn(), updateGlobalTool: vi.fn() } }));
vi.mock('../../components/AppDialogs', () => ({ requestAppConfirm: vi.fn(), showAppToast: vi.fn() }));
vi.mock('../../components/ToolIcon', () => ({ default: () => null }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string, options?: string | Record<string, unknown>) => {
  const value = key.split('.').reduce<unknown>((parent, name) =>
    parent && typeof parent === 'object' ? (parent as Record<string, unknown>)[name] : undefined, en);
  const template = typeof value === 'string' ? value : typeof options === 'string' ? options : key;
  return template.replace(/\{\{(\w+)\}\}/g, (_, name: string) => String(typeof options === 'object' ? options[name] ?? '' : ''));
} }) }));

function elements(node: ReactNode): ReactElement<any>[] {
  if (Array.isArray(node)) return node.flatMap(elements);
  if (!node || typeof node !== 'object' || !('props' in node)) return [];
  const element = node as ReactElement<any>;
  return [element, ...elements(element.props.children)];
}
function view() {
  state.index = 0;
  const nodes = elements(WorkspaceGlobalToolsView({ selectedTenantId: 'company' }));
  if (!state.expanded) {
    const header = nodes.find((node) => node.props.className === 'ws-tools-server-header');
    expect(header).toBeDefined();
    header!.props.onClick();
    return view();
  }
  return nodes;
}
function button(nodes: ReactElement<any>[], label: string) {
  const found = nodes.find((node) => node.type === 'button' && node.props.children === label);
  expect(found, label).toBeDefined();
  return found!;
}

beforeEach(() => {
  vi.clearAllMocks();
  state.editing = false;
  state.expanded = false;
  vi.mocked(requestAppConfirm).mockResolvedValue(true);
  vi.mocked(toolsApi.updateGlobalTool).mockResolvedValue({ enabled: false });
  vi.mocked(toolsApi.listCatalog).mockResolvedValue([state.tool]);
});

describe('company MCP credential recovery consumer', () => {
  it('offers schema-free recovery and submits only explicit scope and the current fingerprint', async () => {
    const nodes = view();
    button(nodes, 'Configure');
    await button(nodes, 'Authorize company sharing').props.onClick();
    expect(requestAppConfirm).toHaveBeenCalledWith(expect.objectContaining({
      message: expect.stringContaining('https://ledger.example/mcp'),
    }));
    expect(toolsApi.updateGlobalTool).toHaveBeenCalledExactlyOnceWith('company-tool', {
      confirm_mcp_credential: true, expected_mcp_fingerprint: 'a'.repeat(64),
    });
    expect(toolsApi.listCatalog).toHaveBeenCalledExactlyOnceWith('company');
    expect(state.tool.enabled).toBe(false);
    expect(showAppToast).toHaveBeenCalledWith('Company sharing scope confirmed.', 'success');
  });

  it('makes no write when the administrator declines the authority declaration', async () => {
    vi.mocked(requestAppConfirm).mockResolvedValue(false);
    await button(view(), 'Authorize company sharing').props.onClick();
    expect(toolsApi.updateGlobalTool).not.toHaveBeenCalled();
    expect(toolsApi.listCatalog).not.toHaveBeenCalled();
  });

  it('shows stale authority failure and reloads the current catalog without claiming success', async () => {
    vi.mocked(toolsApi.updateGlobalTool).mockRejectedValue(new Error('MCP configuration changed; reload'));
    await button(view(), 'Authorize company sharing').props.onClick();
    expect(showAppToast).toHaveBeenCalledExactlyOnceWith('MCP configuration changed; reload', 'error');
    expect(toolsApi.listCatalog).toHaveBeenCalledExactlyOnceWith('company');
  });

  it('offers a password field for an MCP tool without provider-defined config fields', () => {
    state.editing = true;
    const inputs = view().filter((node) => node.type === 'input' && node.props.type === 'password');
    expect(inputs).toHaveLength(1);
    expect(inputs[0].props.value).toBe('********');
  });
});
