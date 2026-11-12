import type { ReactElement, ReactNode } from 'react';
import { beforeEach, expect, it, vi } from 'vitest';
import { toolsApi } from '../../api/domains/tools';
import { showAppToast } from '../../components/AppDialogs';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import WorkspaceGlobalToolsView from './WorkspaceGlobalToolsView';

const state = vi.hoisted(() => ({ index: 0, locale: 'zh' }));
vi.mock('react', async (original) => {
  const actual = await original<typeof import('react')>();
  return { ...actual, useEffect: vi.fn(), useState: (initial: unknown) => {
    const index = state.index++;
    const value = index === 0 ? [] : index === 4 ? true : index === 5
      ? { server_url: 'https://public.example/mcp', server_name: 'Public docs' }
      : index === 7 ? { ok: true, tools: [{ name: 'lookup' }, { name: 'outline' }] } : initial;
    return [value, vi.fn()];
  } };
});
vi.mock('../../api/domains/tools', () => ({ toolsApi: { listCatalog: vi.fn(), createTool: vi.fn() } }));
vi.mock('../../components/AppDialogs', () => ({ showAppToast: vi.fn(), requestAppConfirm: vi.fn() }));
vi.mock('../../components/ToolIcon', () => ({ default: () => null }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string, options?: string | Record<string, unknown>) => {
  const catalog = state.locale === 'zh' ? zh : en;
  const value = key.split('.').reduce<unknown>((parent, name) => parent && typeof parent === 'object'
    ? (parent as Record<string, unknown>)[name] : undefined, catalog);
  const template = typeof value === 'string' ? value : typeof options === 'string' ? options : key;
  return template.replace(/\{\{(\w+)\}\}/g, (_, name: string) => String(typeof options === 'object' ? options[name] ?? '' : ''));
} }) }));

function elements(node: ReactNode): ReactElement<any>[] {
  if (Array.isArray(node)) return node.flatMap(elements);
  if (!node || typeof node !== 'object' || !('props' in node)) return [];
  const element = node as ReactElement<any>;
  return [element, ...elements(element.props.children)];
}

beforeEach(() => {
  state.index = 0;
  vi.clearAllMocks();
  vi.mocked(toolsApi.listCatalog).mockResolvedValue([]);
  vi.mocked(toolsApi.createTool).mockReset();
});

it.each(['zh', 'en'])('reports zero imported tools without inviting review (%s)', async (locale) => {
  state.locale = locale;
  vi.mocked(toolsApi.createTool).mockRejectedValue(new Error('refused'));
  const onMcpImported = vi.fn();
  const label = locale === 'zh' ? '全部导入' : 'Import All';
  const button = elements(WorkspaceGlobalToolsView({ selectedTenantId: 'company', onMcpImported }))
    .find(node => node.type === 'button' && node.props.children === label);
  expect(button).toBeDefined();
  await button!.props.onClick();
  const message = String(vi.mocked(showAppToast).mock.calls[0][0]);
  expect(message).toContain(locale === 'zh' ? '已导入 0/2 个工具，失败：' : 'Imported 0/2 tools. Failed:');
  expect(message).toContain('lookup: refused; outline: refused');
  expect(message).not.toContain(locale === 'zh' ? '审核后使用' : 'Review each');
  expect(onMcpImported).not.toHaveBeenCalled();
});

it.each(['zh', 'en'])('reports partial imports and reviews only successful tools (%s)', async (locale) => {
  state.locale = locale;
  vi.mocked(toolsApi.createTool).mockResolvedValueOnce({}).mockRejectedValueOnce(new Error('refused'));
  const onMcpImported = vi.fn();
  const label = locale === 'zh' ? '全部导入' : 'Import All';
  const button = elements(WorkspaceGlobalToolsView({ selectedTenantId: 'company', onMcpImported }))
    .find(node => node.type === 'button' && node.props.children === label);
  expect(button).toBeDefined();
  await button!.props.onClick();
  const message = String(vi.mocked(showAppToast).mock.calls[0][0]);
  expect(message).toContain(locale === 'zh' ? '已导入 1/2 个工具，失败：outline: refused' : 'Imported 1/2 tools. Failed: outline: refused');
  expect(message).toContain(locale === 'zh' ? '请到 MCP 服务器逐一审核后使用' : 'Review each imported tool in MCP Servers before use.');
  expect(onMcpImported).toHaveBeenCalledExactlyOnceWith();
});
