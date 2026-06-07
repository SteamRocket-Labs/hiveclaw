import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

// Knowledge plane IA tests (backend spec §10 / §12 P8):
// - Knowledge renders from the read model (no raw file parsing in the view)
// - capability candidates only DEEP-LINK to skills/workflows tabs
// - the Raw subview hosts the advanced Markdown browser

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key.split('.').pop() ?? key,
    i18n: { language: 'en' },
  }),
}));

const overviewData = {
  identity: { sections: 2, frozenSections: 0, pendingSoulCandidates: 1, lastUpdated: '' },
  memory: { active: 3, stale: 0, superseded: 1, archived: 2, sensitiveSuppressed: 0 },
  distillers: {
    extractor: { name: 'extractor', state: 'active', last_run_at: '' },
    heartbeat: { name: 'heartbeat', state: 'stale', last_run_at: '' },
    dream: { name: 'dream', state: 'never_ran', last_run_at: '' },
    skillDistiller: { name: 'skill_distiller', state: 'never_ran', last_run_at: '' },
  },
  linkedCapabilities: { skillsReferenced: 1, workflowsReferenced: 1, mcpToolsReferenced: 0, skillCandidates: 1 },
};

const candidatesData = {
  skillCandidates: [
    { entry_id: 'mem_s1', content: 'research workflow', timestamp: '2026-06-01', filename: 'strategies.md', source: 'memory/strategies.md' },
  ],
  workflowCandidates: [
    { entry_id: 'mem_w1', content: 'nightly digest pipeline', timestamp: '2026-06-02', filename: 'strategies.md', source: 'memory/strategies.md' },
  ],
  soulCandidates: [{ candidateId: 'cand1', reason: 'thin evidence', at: '' }],
  heldCurations: [{ at: '2026-06-04T10:00:00+00:00', stage: 'scene_curation', reason: 'no LLM', detail: {} }],
};

vi.mock('@tanstack/react-query', () => ({
  useQuery: ({ queryKey }: { queryKey: unknown[] }) => {
    const kind = String(queryKey[0] ?? '');
    if (kind === 'knowledge-overview') return { data: overviewData };
    if (kind === 'knowledge-candidates') return { data: candidatesData };
    if (kind === 'knowledge-pages') return { data: { pages: [] } };
    if (kind === 'knowledge-entries') return { data: { entries: [] } };
    if (kind === 'knowledge-events') return { data: { events: [] } };
    return { data: undefined };
  },
}));

vi.mock('./AgentMindSection', () => ({
  default: () => <div data-testid="raw-advanced-view">raw markdown browser</div>,
}));

vi.mock('../../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <pre>{content}</pre>,
}));

vi.mock('../../api/domains/knowledge', () => ({
  knowledgeApi: {
    overview: vi.fn(),
    pages: vi.fn(),
    page: vi.fn(),
    entries: vi.fn(),
    events: vi.fn(),
    candidates: vi.fn(),
  },
}));

import AgentKnowledgeSection from './AgentKnowledgeSection';

describe('AgentKnowledgeSection', () => {
  it('renders the structured overview from the read model (no raw file parsing)', () => {
    const html = renderToStaticMarkup(
      <AgentKnowledgeSection agentId="agent-1" canEdit={false} onNavigateTab={() => {}} />,
    );
    expect(html).toContain('Identity');
    expect(html).toContain('Memory');
    expect(html).toContain('Distillers');
    expect(html).toContain('Linked capabilities');
    // A1 (exists ≠ fresh): a stale pipeline renders visibly in warning color,
    // distinct from active green and never_ran tertiary.
    expect(html).toContain('stale');
    expect(html).toContain('#f59e0b');
    // Held curation decisions surface on the overview.
    expect(html).toContain('no LLM');
    // Default view is Overview, not a file browser.
    expect(html).not.toContain('raw markdown browser');
  });

  it('exposes the Raw advanced view only for manage access', () => {
    const html = renderToStaticMarkup(
      <AgentKnowledgeSection agentId="agent-1" canEdit onNavigateTab={() => {}} />,
    );
    for (const view of ['overview', 'pages', 'entries', 'candidates', 'timeline', 'raw']) {
      expect(html).toContain(view);
    }
  });

  it('hides the Raw advanced view for use-only access', () => {
    const html = renderToStaticMarkup(
      <AgentKnowledgeSection agentId="agent-1" canEdit={false} onNavigateTab={() => {}} />,
    );
    expect(html).toContain('overview');
    expect(html).not.toContain('raw');
  });
});
