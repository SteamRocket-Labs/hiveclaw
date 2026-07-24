import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import { translateFromCatalog } from '../../test/i18nMock';

const queryMock = vi.hoisted(() => ({
  state: {
    data: undefined as { content: string } | undefined,
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  },
  options: undefined as { queryKey?: unknown[]; enabled?: boolean } | undefined,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (
      key: string,
      fallbackOrOptions?: string | Record<string, unknown>,
      options?: Record<string, unknown>,
    ) => translateFromCatalog(en, key, fallbackOrOptions, options),
  }),
}));

vi.mock('@tanstack/react-query', () => ({
  useQuery: (options: { queryKey?: unknown[]; enabled?: boolean }) => {
    queryMock.options = options;
    return queryMock.state;
  },
}));

vi.mock('../../components/FileBrowser', () => ({
  default: ({ rootPath }: { rootPath: string }) => <div>files:{rootPath}</div>,
}));

vi.mock('../../api/domains/files', () => ({
  fileApi: {
    read: vi.fn(),
    list: vi.fn(),
    write: vi.fn(),
    delete: vi.fn(),
    download: vi.fn(),
  },
}));

import AgentMindSection from './AgentMindSection';

describe('AgentMindSection current identity', () => {
  beforeEach(() => {
    queryMock.state.data = undefined;
    queryMock.state.isLoading = false;
    queryMock.state.isError = false;
    queryMock.state.refetch.mockReset();
    queryMock.options = undefined;
  });

  it('shows the complete current soul through a read-only owner product surface', () => {
    queryMock.state.data = {
      content: [
        '---',
        'schema: hive.soul.v2',
        '---',
        '<soul_identity frozen="true">',
        'Own the verified outcome.',
        '</soul_identity>',
      ].join('\n'),
    };

    const markup = renderToStaticMarkup(<AgentMindSection agentId="agent-1" canEdit />);

    expect(queryMock.options).toMatchObject({
      queryKey: ['agent-soul', 'agent-1'],
      enabled: true,
    });
    expect(markup).toContain('Current identity');
    expect(markup).toContain('Read only');
    expect(markup).toContain('soul.md');
    expect(markup).toContain('schema: hive.soul.v2');
    expect(markup).toContain('Own the verified outcome.');
    expect(markup).not.toContain('Edit');
  });

  it('distinguishes an unavailable identity read from an empty identity', () => {
    queryMock.state.isError = true;

    const markup = renderToStaticMarkup(<AgentMindSection agentId="agent-1" canEdit />);

    expect(markup).toContain('Current identity could not be loaded.');
    expect(markup).toContain('Try Again');
    expect(markup).not.toContain('No identity content has been created yet.');
  });
});
