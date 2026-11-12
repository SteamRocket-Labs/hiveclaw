import { expect, it } from 'vitest';
import zh from '../../i18n/zh.json';
import en from '../../i18n/en.json';
import { assistantNameForSharedDisplay } from './assistantScope';

const t = (catalog: Record<string, unknown>) => (key: string, _fallback: string, values?: Record<string, unknown>) => {
  const parts = key.split('.');
  let node: any = catalog;
  for (const part of parts) node = node?.[part];
  let text = typeof node === 'string' ? node : key;
  for (const [name, value] of Object.entries(values || {})) {
    text = text.replaceAll(`{{${name}}}`, String(value));
  }
  return text;
};

it('the viewer\'s own default assistant keeps the possessive label in both languages', () => {
  const base = { name: 'My Assistant', isAgent: true, authorAgentId: 'agent-1', viewerAssistantId: 'agent-1' };
  expect(assistantNameForSharedDisplay({ ...base, t: t(zh as any) })).toBe('我的助理');
  expect(assistantNameForSharedDisplay({ ...base, t: t(en as any) })).toBe('My Assistant');
});

it('another member\'s default assistant carries the owner, or stays neutral', () => {
  expect(assistantNameForSharedDisplay({
    name: 'My Assistant', isAgent: true, authorAgentId: 'agent-2', viewerAssistantId: 'agent-1',
    ownerName: '张三', t: t(zh as any),
  })).toBe('张三的助理');
  expect(assistantNameForSharedDisplay({
    name: 'My Assistant', isAgent: true, authorAgentId: 'agent-2', viewerAssistantId: 'agent-1',
    ownerName: null, t: t(zh as any),
  })).toBe('助理');
  expect(assistantNameForSharedDisplay({
    name: 'My Assistant', isAgent: true, authorAgentId: 'agent-2', viewerAssistantId: null,
    ownerName: null, t: t(en as any),
  })).toBe('Assistant');
});

it('a human author and a real custom agent name are never relabeled', () => {
  expect(assistantNameForSharedDisplay({
    name: 'My Assistant', isAgent: false, authorAgentId: 'user-9', viewerAssistantId: 'agent-1',
    ownerName: null, t: t(zh as any),
  })).toBe('My Assistant');
  expect(assistantNameForSharedDisplay({
    name: '周报助手', isAgent: true, authorAgentId: 'agent-3', viewerAssistantId: 'agent-1',
    ownerName: null, t: t(zh as any),
  })).toBe('周报助手');
});
