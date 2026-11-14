// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';

import en from '../i18n/en.json';
import zh from '../i18n/zh.json';
import { translateFromCatalog } from '../test/i18nMock';
import Plaza from './Plaza';

const locale = vi.hoisted(() => ({ value: 'zh' }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string | Record<string, unknown>) =>
      translateFromCatalog(locale.value === 'zh' ? zh : en, key, fallback),
  }),
}));
vi.mock('../stores', () => ({
  useAuthStore: () => ({ user: { id: 'member', tenant_id: 'company', role: 'member', display_name: 'Member' } }),
}));
vi.mock('./assistant/assistantScope', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./assistant/assistantScope')>()),
  useAssistantAgentId: () => 'own-assistant',
}));

const author = { author_id: 'hr', author_type: 'agent', author_name: '__system_hr__' };
const post = {
  id: 'post',
  ...author,
  content: 'Team update',
  likes_count: 0,
  comments_count: 1,
  created_at: '2026-11-14T00:00:00Z',
};
vi.mock('../api/domains/plaza', () => ({
  plazaApi: {
    listPosts: async () => [post],
    getStats: async () => ({
      total_posts: 2,
      total_comments: 1,
      today_posts: 2,
      top_contributors: [
        { name: '__system_hr__', type: 'agent', posts: 1 },
        { name: 'Internal setup', type: 'agent', posts: 1 },
      ],
    }),
    listUsers: async () => [],
    getPost: async () => ({ ...post, comments: [{ id: 'comment', post_id: 'post', ...author, content: 'Welcome' }] }),
  },
}));
vi.mock('../api/domains/agents', () => ({
  agentApi: { list: async () => [{ id: 'hr', name: '__system_hr__', status: 'running' }] },
}));

let queryClient: QueryClient;
afterEach(() => {
  cleanup();
  queryClient.clear();
});

it.each([
  ['zh', '人事助理'],
  ['en', 'HR assistant'],
])('uses the X4 HR name throughout the plaza in %s without hiding the employee', async (language, label) => {
  locale.value = language;
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { container } = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/plaza?post=post']}>
        <Plaza />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await screen.findByText('Internal setup');
  const leaderboard = container.querySelector('.plaza-leaderboard') as HTMLElement;
  expect(within(leaderboard).getByText(label)).toBeTruthy();
  expect(within(leaderboard).getByText('Internal setup')).toBeTruthy();
  expect(container.querySelector('.plaza-post-author-name')?.textContent).toContain(label);
  expect(container.querySelector('.plaza-comment-name')?.textContent).toContain(label);
  expect(screen.getByTitle(label)).toBeTruthy();
  fireEvent.change(container.querySelector('textarea')!, { target: { value: '@' } });
  expect(within(container.querySelector('.plaza-mention-dropdown') as HTMLElement).getByText(label)).toBeTruthy();
  fireEvent.mouseDown(within(container.querySelector('.plaza-mention-dropdown') as HTMLElement).getByText(label));
  expect(container.querySelector('textarea')?.value).toBe(`@${label} `);
  expect(container.textContent).not.toContain('__system_hr__');
});
