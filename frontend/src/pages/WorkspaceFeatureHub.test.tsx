import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';

let automationAgentIdsKey = '';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string, values?: Record<string, unknown>) => {
      const template = fallback ?? key;
      return Object.entries(values || {}).reduce(
        (text, [name, value]) => text.replaceAll(`{{${name}}}`, String(value)),
        template,
      );
    },
  }),
}));

vi.mock('@tanstack/react-query', () => ({
  useQueryClient: () => ({
    invalidateQueries: vi.fn(),
  }),
  useQuery: (options: { queryKey: unknown[] }) => {
    const key = String(options.queryKey[0]);
    if (key === 'agents') {
      return {
        data: [
          {
            id: 'agent-1',
            name: 'Research Lead',
            role_description: 'Market research',
            status: 'running',
            creator_id: 'user-1',
            owner_user_id: 'user-3',
            is_owner: true,
            access_level: 'manage',
            action_capabilities: {
              can_use: true,
              can_manage: true,
              can_manage_permissions: true,
              can_manage_schedule: true,
              can_manage_channel: true,
              can_transfer_ownership: true,
            },
            created_at: '2026-06-20T00:00:00Z',
          },
          {
            id: 'agent-shared',
            name: 'Shared Analyst',
            role_description: 'Company shared research',
            status: 'running',
            creator_id: 'user-2',
            owner_user_id: 'user-2',
            is_owner: false,
            access_level: 'use',
            action_capabilities: {
              can_use: true,
              can_manage: false,
              can_manage_permissions: false,
              can_manage_schedule: false,
              can_manage_channel: false,
              can_transfer_ownership: false,
            },
            created_at: '2026-06-21T00:00:00Z',
          },
        ],
        isLoading: false,
      };
    }
    if (key === 'workflow-definitions') {
      return {
        data: [
          {
            id: 'workflow-1',
            name: 'Weekly market sweep',
            description: 'Collect and synthesize market signals.',
            status: 'active',
            definition_version: 1,
          },
        ],
        isLoading: false,
      };
    }
    if (key === 'feature-hub-automation-rows') {
      automationAgentIdsKey = String(options.queryKey[1] || '');
      return {
        data: [
          {
            id: 'agent-1:trigger-current',
            agentId: 'agent-1',
            agentName: 'Research Lead',
            name: 'Daily railway log check',
            schedule: { kind: 'cron', expr: '0 10 * * 2' },
            status: 'running',
            statusKey: 'running',
            section: 'current',
            href: '/agents/agent-1#aware',
          },
          {
            id: 'agent-1:trigger-paused',
            agentId: 'agent-1',
            agentName: 'Research Lead',
            name: 'Hive H7 Evidence Loop',
            schedule: { kind: 'cron', expr: '0 9 * * *' },
            status: 'paused',
            statusKey: 'paused',
            section: 'paused',
            href: '/agents/agent-1#aware',
          },
        ],
        isLoading: false,
      };
    }
    if (key === 'feature-hub-plans') {
      return {
        data: [
          {
            agentId: 'agent-1',
            agentName: 'Research Lead',
            id: 'plan-1',
            title: 'Confirm product research scope',
            status: 'awaiting_confirmation',
            updatedAt: '2026-06-23T08:00:00Z',
            href: '/agents/agent-1#chat',
          },
        ],
        isLoading: false,
      };
    }
    if (key === 'feature-hub-approvals') {
      return {
        data: [
          {
            id: 'approval-1',
            actionType: 'send_external_message',
            agentName: 'Research Lead',
            status: 'pending',
            createdAt: '2026-06-23T08:30:00Z',
          },
        ],
        isLoading: false,
      };
    }
    if (key === 'feature-hub-memory') {
      return {
        data: [
          {
            agentId: 'agent-1',
            agentName: 'Research Lead',
            active: 12,
            stale: 1,
            pendingSoulCandidates: 2,
            skillCandidates: 3,
            href: '/agents/agent-1#knowledge',
          },
        ],
        isLoading: false,
      };
    }
    return { data: [], isLoading: false };
  },
}));

vi.mock('../stores', () => ({
  useAuthStore: (selector?: any) => {
    const state = {
      user: {
        id: 'user-1',
        username: 'rocky',
        email: 'rocky@example.com',
        display_name: 'rocky',
        role: 'member',
        is_active: true,
        created_at: '2026-06-01T00:00:00Z',
      },
    };
    return selector ? selector(state) : state;
  },
}));

vi.mock('react-router-dom', () => ({
  Link: ({ to, children, className }: any) => (
    <a href={to} className={className}>
      {children}
    </a>
  ),
}));

import WorkspaceFeatureHub, {
  AutomationRowSurface,
  automationScheduleFacts,
  automationScheduleLabel,
  automationStatus,
} from './WorkspaceFeatureHub';
import zh from '../i18n/zh.json';

const recordingT = (keys: string[]) => (key: string, fallback?: string, values?: Record<string, unknown>) => {
  keys.push(key);
  const template = fallback ?? key;
  return Object.entries(values || {}).reduce(
    (text, [name, value]) => text.replaceAll(`{{${name}}}`, String(value)),
    template,
  );
};

describe('WorkspaceFeatureHub', () => {
  beforeEach(() => {
    automationAgentIdsKey = '';
  });

  it('renders automation assets from the real workflow definition adapter', () => {
    const markup = renderToStaticMarkup(<WorkspaceFeatureHub kind="automations" />);

    expect(markup).toContain('Automations');
    expect(markup).toContain('Current');
    expect(markup).toContain('Paused');
    expect(markup).toContain('Daily railway log check');
    expect(markup).toContain('Hive H7 Evidence Loop');
    expect(markup).toContain('Research Lead');
    expect(markup).toContain('Tuesday 10:00');
    expect(markup).toContain('Every day at 09:00');
    expect(markup).toContain('Paused');
    expect(markup).toContain('New automation');
    expect(markup).toContain('href="/agents/agent-1#aware"');
    expect(markup).not.toContain('Weekly market sweep');
    expect(markup).not.toContain('Skill registry');
  });

  it('scopes automation aggregation to server-authorized agents after ownership transfer', () => {
    renderToStaticMarkup(<WorkspaceFeatureHub kind="automations" />);

    expect(automationAgentIdsKey).toBe('agent-1');
    expect(automationAgentIdsKey).not.toContain('agent-shared');
  });

  it('opens the manual automation create dialog on the automation hub', () => {
    const markup = renderToStaticMarkup(<WorkspaceFeatureHub kind="automations" initialAutomationCreateOpen />);

    expect(markup).toContain('role="dialog"');
    expect(markup).toContain('New automation');
    expect(markup).toContain('Automation title');
    expect(markup).toContain('Select agent');
    expect(markup).toContain('Every hour');
    expect(markup).toContain('Every day');
    expect(markup).toContain('Every week');
    expect(markup).toContain('Custom');
    expect(markup).not.toContain('Work Number');
    expect(markup).not.toContain('Reasoning strength');
    expect(markup).not.toContain('Mode');
  });

  it('renders memory governance links without inventing a separate memory product surface', () => {
    const markup = renderToStaticMarkup(<WorkspaceFeatureHub kind="memory" />);

    expect(markup).toContain('Memory &amp; Knowledge');
    expect(markup).toContain('Research Lead');
    expect(markup).toContain('Memory entries');
    expect(markup).toContain('Failure modes (active)');
    expect(markup).toContain('Soul candidates');
    expect(markup).toContain('Skill candidates');
    expect(markup).toContain('href="/agents/agent-1#knowledge"');
    expect(markup).toContain('href="/enterprise/memory"');
  });

  it('renders a cross-agent plan queue instead of only routing users away', () => {
    const markup = renderToStaticMarkup(<WorkspaceFeatureHub kind="plans" />);

    expect(markup).toContain('Confirm product research scope');
    expect(markup).toContain('awaiting_confirmation');
    expect(markup).toContain('href="/agents/agent-1#chat"');
  });

  it('renders the company approval queue from the enterprise approval adapter', () => {
    const markup = renderToStaticMarkup(<WorkspaceFeatureHub kind="approvals" />);

    expect(markup).toContain('send_external_message');
    expect(markup).toContain('pending');
    expect(markup).toContain('Research Lead');
    expect(markup).toContain('href="/enterprise/approvals"');
  });

  it('renders A2A and Team as a real front-end surface over A2A collaborators, subagents, and local channel entry points', () => {
    const markup = renderToStaticMarkup(<WorkspaceFeatureHub kind="team" />);

    expect(markup).toContain('A2A / Team');
    expect(markup).toContain('Research Lead');
    expect(markup).toContain('Session-local team');
    expect(markup).toContain('A2A collaborators');
    expect(markup).toContain('Local Agent Channel');
    expect(markup).toContain('href="/agents/agent-1#a2a"');
    expect(markup).toContain('href="/agents/agent-1#extensions"');
    expect(markup).toContain('href="/enterprise/extensions"');
    expect(markup).toContain('href="/local-agents"');
  });
});

describe('automation schedule/status display (live consumer seam)', () => {
  it('derives schedule facts from the typed trigger config and never renders display_schedule prose or poll URLs', () => {
    const trigger = {
      type: 'poll',
      config: { url: 'https://secret.example/token-raw-88' },
      display_schedule: 'Poll https://secret.example/token-raw-88',
    };

    const facts = automationScheduleFacts(trigger);
    expect(facts).toEqual({ kind: 'poll' });

    const keys: string[] = [];
    const label = automationScheduleLabel(facts, recordingT(keys));
    expect(label).toBe('Polling');
    expect(label).not.toContain('secret.example');
    expect(label).not.toContain('token-raw-88');
    expect(label).not.toContain('Poll ');
    expect(keys).toContain('agent.aware.schedulePollLabel');
  });

  it('renders unknown schedule kinds and unknown attention states as neutral localized labels, never raw codes', () => {
    const scheduleKeys: string[] = [];
    expect(automationScheduleLabel({ kind: 'experimental_future_kind' } as never, recordingT(scheduleKeys))).toBe('Scheduled work');
    expect(scheduleKeys).toContain('featureHub.scheduleGeneric');

    const status = automationStatus({ attention_state: 'experimental_future_state', is_enabled: true }, []);
    expect(status.statusKey).toBe('unknown');
    expect(status.status).toBe('unknown');
    expect(String(status.statusKey)).not.toContain('experimental_future_state');
  });

  it('maps every canonical autonomy attention_state exactly instead of collapsing to unknown', () => {
    // The canonical attention_state vocabulary from autonomy_overview is a
    // closed machine set; every member carries real semantics and must map to
    // its own typed key. Only codes outside this set resolve to unknown.
    expect(automationStatus({ attention_state: 'active', is_enabled: true }, []).statusKey).toBe('active');
    expect(automationStatus({ attention_state: 'expired', is_enabled: true }, []).statusKey).toBe('expired');
    expect(automationStatus({ attention_state: 'max_fires_reached', is_enabled: true }, []).statusKey).toBe('maxFires');
    expect(automationStatus({ attention_state: 'backoff_active', is_enabled: true }, []).statusKey).toBe('backoff');
    expect(automationStatus({ attention_state: 'no_recent_attempt', is_enabled: true }, []).statusKey).toBe('noRecentAttempt');
    expect(automationStatus({ attention_state: 'missing_model', is_enabled: true }, []).statusKey).toBe('missingModel');
    expect(automationStatus({ attention_state: 'failed_recently', is_enabled: true }, []).statusKey).toBe('failed');
    expect(automationStatus({ attention_state: 'needs_reconciliation', is_enabled: true }, []).statusKey).toBe('needsReconciliation');
    // Non-canonical codes stay the honest neutral unknown.
    expect(automationStatus({ attention_state: 'brand_new_backend_code', is_enabled: true }, []).statusKey).toBe('unknown');

    const zhStatus = (zh as any).featureHub.automationStatus;
    expect(zhStatus.expired).toBe('已过期');
    expect(zhStatus.maxFires).toBe('已达运行上限');
    expect(zhStatus.backoff).toBe('冷却中');
    expect(zhStatus.noRecentAttempt).toBe('最近未运行');
    expect(zhStatus.needsReconciliation).toBe('需要管理员处理');
  });

  it('mirrors build_trigger_view precedence: gate states outrank the latest attempt outcome', () => {
    // build_trigger_view resolves paused > expired > max_fires_reached >
    // backoff_active > attempt-derived states. The trigger-level gate is the
    // canonical truth about whether the automation can still run, so a
    // completed/running last attempt must not mask an expired or capped
    // trigger.
    const withAttempt = (attentionState: string, status: string) => [
      { trigger_id: 'trigger-1', status },
    ];
    const trigger = (attentionState: string) => ({ id: 'trigger-1', attention_state: attentionState, is_enabled: true });

    expect(automationStatus(trigger('expired'), withAttempt('expired', 'completed')).statusKey).toBe('expired');
    expect(automationStatus(trigger('max_fires_reached'), withAttempt('max_fires_reached', 'running')).statusKey).toBe('maxFires');
    expect(automationStatus(trigger('backoff_active'), withAttempt('backoff_active', 'completed')).statusKey).toBe('backoff');
    expect(automationStatus(trigger('needs_reconciliation'), withAttempt('needs_reconciliation', 'completed')).statusKey).toBe('needsReconciliation');
    expect(automationStatus(trigger('missing_model'), withAttempt('missing_model', 'completed')).statusKey).toBe('missingModel');
    expect(automationStatus(trigger('failed_recently'), withAttempt('failed_recently', 'completed')).statusKey).toBe('failed');

    // The latest attempt still colors the active tail (and its own evidence).
    expect(automationStatus(trigger('active'), withAttempt('active', 'running')).statusKey).toBe('running');
    expect(automationStatus(trigger('active'), withAttempt('active', 'completed')).statusKey).toBe('completed');
    expect(automationStatus(trigger('active'), withAttempt('active', 'failed')).statusKey).toBe('failed');
    expect(automationStatus({ id: 't', attention_state: 'active', is_enabled: true }, []).statusKey).toBe('active');
    expect(automationStatus({ id: 't', attention_state: 'no_recent_attempt', is_enabled: true }, []).statusKey).toBe('noRecentAttempt');
  });

  it('renders known interval and cron schedules plus statuses through localized en/zh catalog keys', () => {
    // interval
    const intervalKeys: string[] = [];
    expect(automationScheduleLabel({ kind: 'interval', minutes: 30 }, recordingT(intervalKeys))).toBe('Every 30 min');
    expect(automationScheduleLabel({ kind: 'interval', minutes: 120 }, recordingT([]))).toBe('Every 2h');
    expect(intervalKeys).toContain('agent.aware.scheduleEveryMinutes');
    // cron day-at-time
    expect(automationScheduleLabel({ kind: 'cron', expr: '0 9 * * *' }, recordingT([]))).toBe('Every day at 09:00');
    // cron weekday
    expect(automationScheduleLabel({ kind: 'cron', expr: '0 10 * * 2' }, recordingT([]))).toBe('Tuesday 10:00');

    // known statuses map to catalog keys
    expect(automationStatus({ is_enabled: false }, []).statusKey).toBe('paused');
    expect(automationStatus({ is_enabled: true, attention_state: 'missing_model' }, []).statusKey).toBe('missingModel');
    expect(automationStatus({ is_enabled: true, attention_state: 'active' }, []).statusKey).toBe('active');
    expect(
      automationStatus({ id: 'trigger-1', is_enabled: true, attention_state: 'active' }, [
        { trigger_id: 'trigger-1', status: 'running' },
      ]).statusKey,
    ).toBe('running');
    expect(
      automationStatus({ id: 'trigger-1', is_enabled: true, attention_state: 'active' }, [
        { trigger_id: 'trigger-1', status: 'completed' },
      ]).statusKey,
    ).toBe('completed');

    // zh catalog carries real translations for every rendered key
    const zhStatus = (zh as any).featureHub.automationStatus;
    expect(zhStatus.paused).toBe('已暂停');
    expect(zhStatus.running).toBe('运行中');
    expect(zhStatus.failed).toBe('需要处理');
    expect(zhStatus.missingModel).toBe('未配置模型');
    expect(zhStatus.completed).toBe('已完成');
    expect(zhStatus.active).toBe('进行中');
    expect(zhStatus.unknown).toBe('状态不可用');
    expect(zhStatus.unknown).not.toBe('unknown');
  });

  it('renders status labels through static catalog keys, not a dynamic statusKey template', async () => {
    // Static-key contract: every statusKey must have an explicit literal t()
    // call site so the i18n audit never depends on catalog_pattern resolution
    // for canonical states, and no raw code can reach the DOM via a template.
    const { readFileSync } = await import('node:fs');
    const source = readFileSync(new URL('./WorkspaceFeatureHub.tsx', import.meta.url), 'utf8');

    expect(source).not.toContain('automationStatus.${');
    for (const key of [
      'paused', 'running', 'failed', 'missingModel', 'completed', 'active',
      'expired', 'maxFires', 'backoff', 'noRecentAttempt', 'needsReconciliation', 'unknown',
    ]) {
      expect(source).toContain(`featureHub.automationStatus.${key}`);
    }
  });

  it('keeps raw schedule/status markers out of the rendered automation hub DOM', () => {
    // Rows as collectAutomationRows produces them from a leaky trigger: typed
    // facts only — the display_schedule prose and the unknown attention code
    // must never reach text/title/aria/data-*.
    const markerTriggerRow = {
      id: 'agent-1:trigger-marker',
      agentId: 'agent-1',
      agentName: 'Research Lead',
      triggerId: 'trigger-marker',
      name: '',
      schedule: automationScheduleFacts({
        type: 'poll',
        config: { url: 'https://secret.example/token-raw-88' },
        display_schedule: 'Poll https://secret.example/token-raw-88',
      }),
      status: 'unknown',
      statusKey: automationStatus({ attention_state: 'experimental_future_state', is_enabled: true }, []).statusKey,
      section: 'current' as const,
      href: '/agents/agent-1#aware',
      updatedAt: null,
    };

    const markup = renderToStaticMarkup(<AutomationRowSurface row={markerTriggerRow} />);

    expect(markup).not.toContain('secret.example');
    expect(markup).not.toContain('token-raw-88');
    expect(markup).not.toContain('experimental_future_state');
    expect(markup).not.toContain('Poll https');
    expect(markup).toContain('Polling');
    expect(markup).toContain('Status unavailable');
    expect(markup).toContain('Automation');
  });
});
