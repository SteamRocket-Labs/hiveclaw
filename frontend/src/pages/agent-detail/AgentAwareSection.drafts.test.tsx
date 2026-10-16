// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import i18n from '../../i18n';
import AgentAwareSection from './AgentAwareSection';

vi.mock('./PlanQueueSection', () => ({ default: () => null }));
vi.mock('./TeamMemorySummaryCard', () => ({ default: () => null }));
vi.mock('../../api/domains/workflows', () => ({ listWorkflowDefinitions: async () => [] }));
beforeEach(async () => { await i18n.changeLanguage('en'); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
const stored = { id: 'draft', name: 'Weekly evidence review', type: 'cron', reason: 'Compare the original sources.',
  config: { expr: '0 9 * * *' }, is_enabled: false };

function form(overview = { triggers: [] as any[], recent_attempts: [], findings: [] }, showCreate = true) {
  return <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <AgentAwareSection agentId="employee" awareTriggers={[]} reflectionSessions={[]} reflectionMessages={{}}
      autonomyOverview={overview}
      expandedReflection={null} showAllTriggers={false} reflectionPage={0}
      onSetExpandedReflection={vi.fn()} onSetReflectionMessages={vi.fn()} onSetShowAllTriggers={vi.fn()}
      onSetReflectionPage={vi.fn()} onRefetchTriggers={vi.fn()} initialShowCreateWake={showCreate} />
  </QueryClientProvider>;
}

function fill() {
  fireEvent.change(screen.getByPlaceholderText('Automation title'), { target: { value: stored.name } });
  fireEvent.change(screen.getByPlaceholderText('Add prompt, for example: check production logs and summarize anomalies'),
    { target: { value: stored.reason } });
}

it('saves a paused routine and retries readback without another save or implicit plan decision', async () => {
  const requests: Array<{ url: string; body?: any }> = [];
  let reads = 0;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, init) => {
    const path = String(url);
    requests.push({ url: path, ...(init?.body ? { body: JSON.parse(String(init.body)) } : {}) });
    if (path === '/api/agents') return response([{ id: 'employee', name: 'Research employee' }]);
    if (init?.method === 'POST') return response(stored, 201);
    if (path.endsWith('/triggers')) return ++reads === 1 ? response({ detail: 'Readback unavailable' }, 503) : response([stored]);
    throw new Error(`Unexpected request ${path}`);
  });
  const view = render(form()); fill();
  fireEvent.click(screen.getByRole('button', { name: /^(Create|Save paused routine)$/ }));
  await screen.findByText('Readback unavailable');
  expect(requests.filter((r) => r.body)).toEqual([{ url: '/api/agents/employee/triggers', body: {
    name: stored.name, type: stored.type, reason: stored.reason,
    config: { expr: '0 9 * * *', trigger_class: 'scheduled_job' }, is_enabled: false,
  } }]);
  expect((screen.getByPlaceholderText('Automation title') as HTMLInputElement).value).toBe(stored.name);
  fireEvent.click(screen.getByRole('button', { name: 'Read saved routine' }));
  await screen.findByText('Paused routine saved and read back. Review it before enabling.');
  expect(requests.filter((r) => r.body)).toHaveLength(1);
  expect(screen.queryByRole('dialog')).toBeNull();
  view.rerender(form({ triggers: [{ id: stored.id, display_title: stored.name, attention_state: 'paused' }], recent_attempts: [], findings: [] }, false));
  fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
  expect(screen.queryByText('Paused routine saved and read back. Review it before enabling.')).toBeNull();
});

it('keeps the same draft identity after an unknown save outcome', async () => {
  const writes: any[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, init) => {
    if (String(url) === '/api/agents') return response([{ id: 'employee', name: 'Research employee' }]);
    if (init?.body) {
      writes.push(JSON.parse(String(init.body)));
      if (writes.length === 1) throw new Error('Connection lost after receipt');
      return response({ ...stored, name: writes[0].name }, 201);
    }
    return response([{ ...stored, name: writes[0].name }]);
  });
  render(form());
  fireEvent.click(screen.getByRole('button', { name: 'Save paused routine' }));
  await screen.findByText('Connection lost after receipt');
  fireEvent.click(screen.getByRole('button', { name: 'Save paused routine' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(writes).toHaveLength(2);
  expect(writes[0]).toEqual(writes[1]);
  expect(writes[0].is_enabled).toBe(false);
});

it('recovers a revised routine through a fresh explicit authorization decision', async () => {
  const writes: Array<{ url: string; body?: any }> = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, init) => {
    writes.push({ url: String(url), ...(init?.body ? { body: JSON.parse(String(init.body)) } : {}) });
    return response({ id: 'fresh-review' });
  });
  render(form({ triggers: [{ id: 'routine', display_title: stored.name, display_kind: 'scheduled_job',
    attention_state: 'waiting_approval', next_action: 'review_wake_plan' }], recent_attempts: [], findings: [] }, false));
  fireEvent.click(screen.getByRole('button', { name: 'Review routine authorization' }));
  expect(writes).toHaveLength(0);
  fireEvent.click(screen.getByRole('button', { name: 'Enable without a plan' }));
  await waitFor(() => expect(writes).toHaveLength(3));
  expect(writes[0].url).toBe('/api/agents/employee/plan-recommendations');
  expect(writes[0].body.action_kind).toBe('enable_autonomous_wake');
  expect(writes[1].url).toBe('/api/agents/employee/plan-recommendations/fresh-review/decline');
  expect(writes[2]).toEqual({ url: '/api/agents/employee/triggers/routine', body: {
    is_enabled: true, plan_mode_decision: 'declined', plan_recommendation_id: 'fresh-review',
  } });
});
