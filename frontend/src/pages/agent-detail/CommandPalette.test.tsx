import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import CommandPalette, { defaultCommandArguments, filterCommandIndex } from './CommandPalette';
import type { CommandIndexEntry } from '../../api/domains/ccParity';

const queryHarness = vi.hoisted(() => ({
  calls: [] as Array<{ queryKey: unknown[]; enabled?: boolean }>,
  commands: [
    {
      name: 'goal_start',
      aliases: [],
      description: 'Start a goal',
      category: 'goal',
      source: 'builtin',
      execution_mode: 'runtime',
      permission_mode: 'default',
      bridge_safe: true,
      remote_safe: true,
    },
    {
      name: 'diff',
      aliases: [],
      description: 'Inspect workspace changes',
      category: 'coding_pack',
      source: 'plugin',
      execution_mode: 'external',
      permission_mode: 'coding_pack',
      bridge_safe: false,
      remote_safe: false,
    },
  ] as CommandIndexEntry[],
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string) => fallback || _key,
  }),
}));

vi.mock('@tanstack/react-query', () => ({
  useQuery: (options: { queryKey: unknown[]; enabled?: boolean }) => {
    queryHarness.calls.push(options);
    if (String(options.queryKey[0]) === 'command-palette') {
      return { data: queryHarness.commands, isLoading: false, isError: false, error: null };
    }
    if (String(options.queryKey[0]) === 'command-schema') {
      return { data: undefined, isLoading: false, isError: false, error: null };
    }
    return { data: undefined, isLoading: false, isError: false, error: null };
  },
}));

describe('CommandPalette', () => {
  beforeEach(() => {
    queryHarness.calls.length = 0;
  });

  it('renders user-visible commands including the optional coding pack when opened', () => {
    const markup = renderToStaticMarkup(<CommandPalette agentId="agent-1" sessionId="session-1" initialOpen />);

    expect(markup).toContain('goal_start');
    expect(markup).toContain('diff');
    const listCall = queryHarness.calls.find((call) => String(call.queryKey[0]) === 'command-palette');
    expect(listCall?.enabled).toBe(true);
  });

  it('does not load commands when there is no agent id', () => {
    renderToStaticMarkup(<CommandPalette agentId={null} sessionId="session-1" initialOpen />);

    const listCall = queryHarness.calls.find((call) => String(call.queryKey[0]) === 'command-palette');
    expect(listCall?.enabled).toBe(false);
  });

  it('filters commands across command metadata', () => {
    const filtered = filterCommandIndex(queryHarness.commands, 'coding');

    expect(filtered.map((command) => command.name)).toEqual(['diff']);
  });

  it('builds command-specific argument templates', () => {
    expect(defaultCommandArguments(queryHarness.commands[0])).toContain('"objective"');
    expect(defaultCommandArguments(queryHarness.commands[1])).toBe('{}');
  });
});
