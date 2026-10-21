// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ComponentProps, Dispatch, SetStateAction } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '../../i18n';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';

vi.mock('./PlanQueueSection', () => ({ default: () => null }));
vi.mock('./TeamMemorySummaryCard', () => ({ default: () => null }));
vi.mock('../../api/domains/workflows', () => ({ listWorkflowDefinitions: vi.fn(async () => []) }));
vi.mock('../../api/domains/triggers', () => ({
  triggerApi: { reclaim: vi.fn(), update: vi.fn(), delete: vi.fn() },
}));
vi.mock('../../api/domains/plans', () => ({
  planApi: {
    createRecommendation: vi.fn(async () => ({ id: 'rec-1' })),
    declineRecommendation: vi.fn(async () => ({ id: 'rec-1' })),
  },
}));
vi.mock('../../api/domains/agents', () => ({ agentApi: { list: vi.fn() } }));
vi.mock('../../api/domains/autonomy', () => ({ autonomyApi: { getRuntimeArtifact: vi.fn() } }));
vi.mock('../../api/domains/chat', () => ({ chatApi: {} }));

import { planApi } from '../../api/domains/plans';
import { triggerApi } from '../../api/domains/triggers';
import AgentAwareSection from './AgentAwareSection';

const reclaim = vi.mocked(triggerApi.reclaim);
const update = vi.mocked(triggerApi.update);
const createRecommendation = vi.mocked(planApi.createRecommendation);
const declineRecommendation = vi.mocked(planApi.declineRecommendation);

const NEW_WAKE_COPY = [
  'stateAuthorityRevoked',
  'reasonAuthorityRevoked',
  'reclaimAuthority',
  'reclaimFailed',
  'reviewInPlanMode',
  'enableWithoutPlan',
  'enableWithoutPlanTitle',
  'enableWithoutPlanRequest',
  'enableFailed',
  'reviewPlanFailed',
  'reviewPlanLead',
  'reviewPlanSchedule',
  'reviewPlanCause',
  'reviewPlanIntent',
] as const;

beforeEach(async () => {
  await i18n.changeLanguage('en');
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderSection(overrides: Partial<ComponentProps<typeof AgentAwareSection>> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const props = {
    agentId: 'agent-1',
    awareTriggers: [] as any[],
    reflectionSessions: [],
    reflectionMessages: {},
    expandedReflection: null,
    showAllTriggers: false,
    reflectionPage: 0,
    onSetExpandedReflection: vi.fn() as Dispatch<SetStateAction<string | null>>,
    onSetReflectionMessages: vi.fn() as Dispatch<SetStateAction<Record<string, any[]>>>,
    onSetShowAllTriggers: vi.fn() as Dispatch<SetStateAction<boolean>>,
    onSetReflectionPage: vi.fn() as Dispatch<SetStateAction<number>>,
    onRefetchTriggers: () => undefined,
    autonomyOverview: {
      triggers: [{
        id: 'trigger-1',
        display_kind: 'scheduled_job',
        display_title: 'Daily brief',
        attention_state: 'authority_revoked',
        next_action: 'reclaim_trigger_authority',
      }],
      recent_attempts: [],
      findings: [],
    } as any,
    ...overrides,
  };
  return render(
    <QueryClientProvider client={client}>
      <AgentAwareSection {...props} />
    </QueryClientProvider>,
  );
}

describe('AgentAwareSection reclaim failure', () => {
  it('shows the restore failure instead of leaving the button silent', async () => {
    reclaim.mockRejectedValue(new Error('reclaim refused'));
    renderSection();
    fireEvent.click(screen.getByRole('button', { name: 'Restore authority' }));
    expect((await screen.findByRole('alert')).textContent).toContain('Trigger authority could not be restored.');
    await waitFor(() => expect(reclaim).toHaveBeenCalledWith('agent-1', 'trigger-1'));
  });
});

describe('AgentAwareSection wake enable', () => {
  it.each(['ordinary-owner', 'scoped-admin'])(
    'completes restore for %s through a fresh declined recommendation',
    async () => {
      update.mockResolvedValue({} as never);
      renderSection({
        autonomyOverview: {
          triggers: [{
            id: 'trigger-1',
            display_kind: 'scheduled_job',
            display_title: 'Daily brief',
            attention_state: 'paused',
            next_action: 'resume_wake_policy',
          }],
          recent_attempts: [],
          findings: [],
        },
      });
      fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
      expect(screen.getByTestId('wake-enable-decision')).toBeTruthy();
      fireEvent.click(screen.getByRole('button', { name: 'Enable without a plan' }));
      await waitFor(() => expect(update).toHaveBeenCalledWith('agent-1', 'trigger-1', {
        is_enabled: true,
        plan_mode_decision: 'declined',
        plan_recommendation_id: 'rec-1',
      }));
      expect(createRecommendation).toHaveBeenCalledWith('agent-1', expect.objectContaining({
        action_kind: 'enable_autonomous_wake',
        session_id: 'trigger-authority:agent-1:trigger-1',
        source: 'agent_aware',
      }));
      expect(declineRecommendation).toHaveBeenCalledWith('agent-1', 'rec-1');
    },
  );

  it('shows the enable failure and does not mark the trigger enabled', async () => {
    declineRecommendation.mockRejectedValueOnce(new Error('plan gate'));
    renderSection({
      autonomyOverview: {
        triggers: [{
          id: 'trigger-1',
          display_kind: 'scheduled_job',
          display_title: 'Daily brief',
          attention_state: 'paused',
          next_action: 'resume_wake_policy',
        }],
        recent_attempts: [],
        findings: [],
      },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
    fireEvent.click(screen.getByRole('button', { name: 'Enable without a plan' }));
    expect((await screen.findByRole('alert')).textContent).toContain(
      'Wake policy could not be enabled. Review a plan or try again.',
    );
    expect(update).not.toHaveBeenCalled();
  });

  it('leaves pause as a direct update', async () => {
    update.mockResolvedValue({} as never);
    renderSection({
      autonomyOverview: {
        triggers: [{
          id: 'trigger-1',
          display_kind: 'scheduled_job',
          display_title: 'Daily brief',
          attention_state: 'active',
          next_action: 'pause_wake_policy',
        }],
        recent_attempts: [],
        findings: [],
      },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }));
    await waitFor(() => expect(update).toHaveBeenCalledWith('agent-1', 'trigger-1', { is_enabled: false }));
    expect(createRecommendation).not.toHaveBeenCalled();
  });

  it('opens Plan Mode for the selected wake and still allows an explicit decline', async () => {
    update.mockResolvedValue({} as never);
    const onReviewWakeInPlanMode = vi.fn(async (_request: string) => undefined);
    renderSection({
      autonomyOverview: null,
      onReviewWakeInPlanMode,
      awareTriggers: [{
        id: 'trigger-2',
        name: 'weekly',
        type: 'cron',
        reason: 'Monday digest',
        is_enabled: false,
        fire_count: 1,
        config: { expr: '0 9 * * 1' },
      }],
    });
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
    fireEvent.click(screen.getByRole('button', { name: 'Review in Plan Mode' }));
    await waitFor(() => expect(onReviewWakeInPlanMode).toHaveBeenCalledTimes(1));
    const request = String(onReviewWakeInPlanMode.mock.calls[0][0]);
    expect(request).toContain('trigger-2');
    expect(request).toContain('weekly');
    expect(request).toContain('0 9 * * 1');
    expect(request).toContain('Monday digest');
    expect(request).toContain('Do not enable it until I confirm the plan.');
    expect(createRecommendation).not.toHaveBeenCalled();
    expect(update).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Enable without a plan' }));
    await waitFor(() => expect(update).toHaveBeenCalledWith('agent-1', 'trigger-2', {
      is_enabled: true,
      plan_mode_decision: 'declined',
      plan_recommendation_id: 'rec-1',
    }));
  });

  it('shows a visible failure when Plan Mode cannot be opened', async () => {
    const onReviewWakeInPlanMode = vi.fn(async () => {
      throw new Error('session create failed');
    });
    renderSection({
      autonomyOverview: null,
      onReviewWakeInPlanMode,
      awareTriggers: [{
        id: 'trigger-2',
        name: 'weekly',
        type: 'cron',
        is_enabled: false,
        fire_count: 1,
        config: { expr: '0 9 * * 1' },
      }],
    });
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
    fireEvent.click(screen.getByRole('button', { name: 'Review in Plan Mode' }));
    expect((await screen.findByRole('alert')).textContent).toContain(
      'Plan Mode could not be opened for this wake policy.',
    );
    expect(update).not.toHaveBeenCalled();
    expect(createRecommendation).not.toHaveBeenCalled();
  });

  it('renders the new wake authority copy in Chinese', async () => {
    await i18n.changeLanguage('zh');
    renderSection();
    expect(screen.getByRole('button', { name: '恢复权限' })).toBeTruthy();
    expect(screen.getByText('权限已撤销')).toBeTruthy();
    expect(screen.getByText('触发器权限已撤销。恢复权限后才能运行这次唤醒。')).toBeTruthy();
  });
});

describe('wake authority copy', () => {
  it('has distinct zh and en entries for the new state, reasons, buttons, and errors', () => {
    for (const key of NEW_WAKE_COPY) {
      const english = en.agent.aware[key];
      const chinese = zh.agent.aware[key];
      expect(english).toBeTruthy();
      expect(chinese).toBeTruthy();
      expect(chinese).not.toEqual(english);
    }
  });
});

describe('actual routine work', () => {
  it.each([
    ['awaiting_user_clarification', 'Waiting for your answer'],
    ['running', 'Running'],
    ['failed', 'Run failed'],
    ['completed', 'Run completed'],
  ])('shows %s separately from the completed delivery receipt', (status, label) => {
    renderSection({ autonomyOverview: {
      triggers: [{ id: 'trigger-1', display_kind: 'scheduled_job', display_title: 'Daily brief',
        attention_state: 'active', last_attempt: { status: 'completed', display_summary: 'Delivered into original work',
          work_execution: { status, work_url: '/agents/agent-1/sessions/work-1' } } }],
      recent_attempts: [], findings: [],
    } });
    expect(screen.getByText(`Work execution: ${label}`)).toBeTruthy();
    expect(screen.getByText('Delivered into original work')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Open original work' }).getAttribute('href'))
      .toBe('/agents/agent-1/sessions/work-1');
  });

  it('keeps an unverified work link unavailable', () => {
    renderSection({ autonomyOverview: {
      triggers: [{ id: 'trigger-1', display_kind: 'scheduled_job', display_title: 'Daily brief',
        attention_state: 'active', last_attempt: { status: 'completed', work_execution: { status: 'unavailable' } } }],
      recent_attempts: [], findings: [],
    } });
    expect(screen.getByText('Work execution: Work status unavailable')).toBeTruthy();
    expect(screen.queryByRole('link', { name: 'Open original work' })).toBeNull();
  });
});
