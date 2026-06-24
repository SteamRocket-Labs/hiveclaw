import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import SlashCommandMenu from './SlashCommandMenu';
import type { CommandIndexEntry } from '../../api/domains/ccParity';

const queryHarness = vi.hoisted(() => ({
  calls: [] as Array<{ queryKey: unknown[]; enabled?: boolean }>,
  commands: [
    {
      name: 'goal_start',
      aliases: ['goal'],
      description: 'Start a session goal',
      category: 'goal',
      source: 'builtin',
      execution_mode: 'runtime',
      permission_mode: 'default',
      bridge_safe: true,
      remote_safe: true,
    },
    {
      name: 'team_create',
      aliases: ['team'],
      description: 'Create an enterable agent team',
      category: 'team',
      source: 'builtin',
      execution_mode: 'runtime',
      permission_mode: 'default',
      bridge_safe: true,
      remote_safe: true,
    },
    ...Array.from({ length: 9 }, (_, index) => ({
      name: `task_helper_${index}`,
      aliases: [],
      description: `Task helper ${index}`,
      category: 'task',
      source: 'builtin',
      execution_mode: 'runtime',
      permission_mode: 'default',
      bridge_safe: true,
      remote_safe: true,
    })),
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
    if (String(options.queryKey[0]) === 'slash-command-menu') {
      return { data: queryHarness.commands, isLoading: false, isError: false, error: null };
    }
    return { data: undefined, isLoading: false, isError: false, error: null };
  },
}));

describe('SlashCommandMenu', () => {
  beforeEach(() => {
    queryHarness.calls.length = 0;
  });

  it('does not load commands until the composer starts with slash', () => {
    renderToStaticMarkup(
      <SlashCommandMenu
        agentId="agent-1"
        sessionId="session-1"
        inputValue="normal message"
        disabled={false}
        onPickCommand={() => undefined}
      />,
    );

    const listCall = queryHarness.calls.find((call) => String(call.queryKey[0]) === 'slash-command-menu');
    expect(listCall?.enabled).toBe(false);
  });

  it('renders filtered command suggestions for slash input', () => {
    const markup = renderToStaticMarkup(
      <SlashCommandMenu
        agentId="agent-1"
        sessionId="session-1"
        inputValue="/team"
        disabled={false}
        onPickCommand={() => undefined}
      />,
    );

    expect(markup).toContain('data-testid="slash-command-menu"');
    expect(markup).toContain('team_create');
    expect(markup).not.toContain('goal_start');
    const listCall = queryHarness.calls.find((call) => String(call.queryKey[0]) === 'slash-command-menu');
    expect(listCall?.enabled).toBe(true);
  });

  it('does not truncate the slash command list before all matching task commands are visible', () => {
    const markup = renderToStaticMarkup(
      <SlashCommandMenu
        agentId="agent-1"
        sessionId="session-1"
        inputValue="/task"
        disabled={false}
        onPickCommand={() => undefined}
      />,
    );

    expect(markup).toContain('task_helper_0');
    expect(markup).toContain('task_helper_8');
  });
});
