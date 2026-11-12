import type { TFunction } from 'i18next';
import { expect, it } from 'vitest';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import { toolStepLabel } from './toolStepLabel';

const t = ((key: string, fallback?: string) => translateFromCatalog(zh, key, fallback)) as TFunction;

it('identifies an imported MCP invocation as using a connected application', () => {
  for (const name of ['mcp_lookup', 'mcp__documents__lookup']) {
    const label = toolStepLabel(name, t);
    expect(label).toBe(zh.agent.chat.disclosure.toolTitle.useConnection);
    expect(label).not.toContain(name);
  }
});
