import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';

// 进化 tab tests: the first screen answers "有没有变强、有什么等我批".
// - growth report renders real metrics (failure-mode recurrence/avoidance)
// - held+requires_owner_approval soul candidates get approve/reject actions
//   for manage access (the doorbell), read-only otherwise
// - provisional skills surface as "on trial"

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (
      key: string,
      fallbackOrOptions?: string | Record<string, unknown>,
      options?: Record<string, unknown>,
    ) => translateFromCatalog(zh, key, fallbackOrOptions, options),
    i18n: { language: 'zh' },
  }),
}));

const evolutionData = {
  schema: 'agent_evolution_view.v2',
  path_contract: {},
  lanes: {},
  timeline: [],
  memory_learning: { pending_t3_jobs: [], t3_targets: {} },
  soul: {
    active_path: 'soul.md',
    pending_candidates: [
      {
        candidate_id: 'soulcand-1',
        status: 'held',
        reason: 'candidate requires owner/company approval',
        requires_owner_approval: true,
      },
      { candidate_id: 'soulcand-auto', status: 'committed', reason: 'ok' },
    ],
  },
  skill_ecosystem: {
    summary: { total: 1, active: 0, provisional: 1, rolled_back: 0, needs_review: 0, stale: 0, archived: 0, evolvable: 1, by_origin: {} },
    skills: [
      {
        skill_name: 'deploy-checklist',
        skill_origin: 't3_auto_created',
        evolvable: true,
        state: 'provisional',
        use_count: 2,
        last_candidate_id: 'skill-trial-1',
        trial: { state: 'provisional', positive_count: 2, positive_threshold: 3, negative_count: 0, negative_threshold: 2, window_days: 14 },
      },
    ],
  },
  skill_tuning: {
    candidates: [
      { candidate_id: 'skill-trial-1', skill_name: 'deploy-checklist', status: 'provisional' },
      { candidate_id: 'skill-old', skill_name: 'old-skill', status: 'promoted' },
    ],
  },
  legacy_audit: { detected_legacy_files: [] },
};

const observabilityData = {
  debt: {},
  debt_history: [],
  label_axes: {},
  growth: {
    generated_at: '2026-07-02T11:00:00+00:00',
    metrics: {
      generated_at: '2026-07-02T11:00:00+00:00',
      failure_modes: [
        { id: 'fm-guessing', title: '需求含糊时爱猜', status: 'active', recurred: 1, avoided: 3, avoidance_rate: 0.75 },
      ],
      reuse: { knowledge_pages: 4, total_citations: 9, top_cited: [] },
      rework: { labeled_packages: 10, rework_packages: 1, recent_rate: 0.1, previous_rate: 0.5 },
      feedback_polarity: { recent: { useful: 5, misleading: 0 }, previous: { useful: 2, misleading: 1 } },
      task_volume: { recent_invocations: 12, window_days: 7 },
      evolution: { promotions: 2, rollbacks: 0 },
    },
    report_path: 'memory/control/growth_report.md',
  },
};

vi.mock('@tanstack/react-query', () => ({
  useQuery: ({ queryKey }: { queryKey: unknown[] }) => {
    const kind = String(queryKey[0] ?? '');
    if (kind === 'agent-evolution') return { data: evolutionData, isLoading: false, isError: false };
    if (kind === 'memory-observability') return { data: observabilityData, isLoading: false, isError: false };
    return { data: undefined, isLoading: false, isError: false };
  },
  useMutation: () => ({ mutate: vi.fn(), isPending: false }),
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}));

vi.mock('../../api/domains/evolution', () => ({
  evolutionApi: { get: vi.fn(), approveSoulCandidate: vi.fn(), rejectSoulCandidate: vi.fn() },
}));

vi.mock('../../api/domains/knowledge', () => ({
  knowledgeApi: { observability: vi.fn() },
}));

import AgentEvolutionSection from './AgentEvolutionSection';

describe('AgentEvolutionSection', () => {
  it('renders the growth report from production metrics', () => {
    const html = renderToStaticMarkup(<AgentEvolutionSection agentId="agent-1" active canManage={false} />);
    expect(html).toContain('成长报告');
    expect(html).toContain('需求含糊时爱猜');
    expect(html).toContain('75%'); // avoidance rate
    expect(html).toContain('知识复用');
    expect(html).toContain('返工率');
  });

  it('gives manage access real approve/reject actions on held owner-approval candidates', () => {
    const html = renderToStaticMarkup(<AgentEvolutionSection agentId="agent-1" active canManage />);
    expect(html).toContain('待你审批');
    expect(html).toContain('soulcand-1');
    expect(html).toContain('批准');
    expect(html).toContain('驳回');
    // auto-committed candidates never appear in the approval queue
    expect(html).not.toContain('soulcand-auto');
  });

  it('keeps the approval queue read-only without manage access', () => {
    const html = renderToStaticMarkup(<AgentEvolutionSection agentId="agent-1" active canManage={false} />);
    expect(html).toContain('待你审批');
    // hint copy mentions 批准; the action BUTTON must be absent
    expect(html).not.toContain('批准</button>');
  });

  it('surfaces provisional skills as on-trial', () => {
    const html = renderToStaticMarkup(<AgentEvolutionSection agentId="agent-1" active canManage={false} />);
    expect(html).toContain('技能试用中');
    expect(html).toContain('deploy-checklist');
    expect(html).toContain('provisional');
    expect(html).toContain('2 / 3');
    expect(html).toContain('0 / 2');
  });
});
