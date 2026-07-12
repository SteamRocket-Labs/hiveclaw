import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

// 记忆 tab IA tests (two-plane world, memory spec v1.2):
// - overview renders per-plane counts + failure-mode lifecycle + pipeline health
// - the retired flat-T3 counters (Active/Superseded/Archived) never resurface
// - plane subviews replace the dead "entries" view; Raw stays manage-only
// - Personal KB is its own library lane, not mixed into agent Memory raw files

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key.split('.').pop() ?? key,
    i18n: { language: 'en' },
  }),
}));

const overviewData = {
  identity: { sections: 2, frozenSections: 0, pendingSoulCandidates: 1, lastUpdated: '' },
  planes: {
    self: { entries: 3, failureModes: { active: 1, mitigating: 1, resolved: 0 } },
    profiles: { entries: 2 },
    knowledge: { pages: 4 },
    milestones: { pages: 1 },
    explicit: { active: 2 },
  },
  pipeline: { pendingPackages: 3, heldJobs: 0, stalled: true, lastAssessedAt: '2026-07-02T10:00:00+00:00' },
  growth: { generatedAt: '2026-07-02T11:00:00+00:00', reportPath: 'memory/control/growth_report.md' },
  distillers: {
    t2_pipeline: { name: 't2_pipeline', state: 'active', last_run_at: '' },
    heartbeat: { name: 'heartbeat', state: 'stale', last_run_at: '' },
    dream: {
      name: 'dream',
      state: 'never_ran',
      last_run_at: '',
      runtime_status: 'pending',
      runtime_task_id: 'dream-task-1',
    },
    skillDistiller: { name: 'skill_distiller', state: 'never_ran', last_run_at: '' },
  },
  linkedCapabilities: { skillsReferenced: 1, workflowsReferenced: 1, mcpToolsReferenced: 0, skillCandidates: 1 },
};

const selfEntry = {
  id: 'fm-guessing',
  file: 'memory/self/self.md',
  category: 'profile_plane',
  content: '### 需求含糊时爱猜 — active\n<!-- id: fm-guessing -->\n- 状态: active\n- 证据: t2-a1b2',
  preview: '需求含糊时爱猜',
  timestamp: '',
  heat: 0,
  recallCount: 0,
  lastRecalledAt: '',
  sensitivity: '',
  status: 'active',
  containerCandidate: '',
  promotedTo: '',
  load: '',
};

const profileEntry = {
  ...selfEntry,
  id: 'pref-lang',
  file: 'memory/profiles/owner.md',
  content: '### 中文汇报 — 已确认\n<!-- id: pref-lang -->\n偏好中文汇报。',
  preview: '偏好中文汇报。',
  status: '',
};

vi.mock('@tanstack/react-query', () => ({
  useQuery: ({ queryKey }: { queryKey: unknown[] }) => {
    const kind = String(queryKey[0] ?? '');
    if (kind === 'knowledge-overview') return { data: overviewData };
    if (kind === 'knowledge-pages') return { data: { pages: [] } };
    if (kind === 'knowledge-entries') return { data: { entries: [selfEntry, profileEntry] } };
    if (kind === 'knowledge-events') return { data: { events: [] } };
    if (kind === 'knowledge-personal-documents') return { data: { documents: [] } };
    if (kind === 'knowledge-personal-search') return { data: { results: [] } };
    return { data: undefined };
  },
  useMutation: () => ({ mutate: vi.fn(), isPending: false }),
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
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
    observability: vi.fn(),
    personalDocuments: vi.fn(),
    personalIngest: vi.fn(),
    personalSearch: vi.fn(),
    personalDocument: vi.fn(),
  },
}));

import AgentKnowledgeSection, { PersonalKnowledgeView } from './AgentKnowledgeSection';

describe('AgentKnowledgeSection', () => {
  it('renders the two-plane overview with failure-mode lifecycle and pipeline health', () => {
    const html = renderToStaticMarkup(
      <AgentKnowledgeSection agentId="agent-1" canEdit={false} onNavigateTab={() => {}} />,
    );
    expect(html).toContain('自我认知');
    expect(html).toContain('记忆版图');
    expect(html).toContain('人际与领域');
    expect(html).toContain('知识网络');
    expect(html).toContain('里程碑');
    // failure-mode lifecycle is the "getting stronger" progress bar
    expect(html).toContain('规避中');
    // pipeline health: a stalled consolidation shows loudly
    expect(html).toContain('消化停滞');
    // growth freshness surfaces
    expect(html).toContain('成长报告更新于');
    // pending soul approvals deep-link to the evolution tab
    expect(html).toContain('去审批');
    // the retired flat-T3 counters never resurface
    expect(html).not.toContain('Superseded');
    expect(html).not.toContain('Archived');
    expect(html).not.toContain('extractor');
    // A1 (exists ≠ fresh): stale renders in warning color (class-driven → var(--warning))
    expect(html).toContain('agent-knowledge-distiller-stale');
    expect(html).toContain('Queued');
    expect(html).toContain('agent-knowledge-distiller-queued');
    // Default view is Overview, not a file browser.
    expect(html).not.toContain('raw markdown browser');
  });

  it('replaces the dead entries view with plane subviews', () => {
    const html = renderToStaticMarkup(
      <AgentKnowledgeSection agentId="agent-1" canEdit onNavigateTab={() => {}} />,
    );
    for (const view of ['overview', 'self', 'profiles', 'personal', 'knowledge', 'milestones', 'timeline', 'raw']) {
      expect(html).toContain(view);
    }
    // the dead subview BUTTONS are gone (copy like 'Skill candidates' may remain)
    expect(html).not.toContain('>entries</button>');
    expect(html).not.toContain('>candidates</button>');
  });

  it('hides the Raw advanced view for use-only access', () => {
    const html = renderToStaticMarkup(
      <AgentKnowledgeSection agentId="agent-1" canEdit={false} onNavigateTab={() => {}} />,
    );
    expect(html).toContain('overview');
    expect(html).not.toContain('raw');
  });

  it('renders the Personal KB lane as read-only search, library, and source refs inside Agent Detail', () => {
    const html = renderToStaticMarkup(
      <PersonalKnowledgeView
        documents={[
          {
            document_id: 'doc-1',
            title: 'Taste notes',
            source_kind: 'paste',
            source_uri: 'clipboard://taste',
            source_sha256: 'a'.repeat(64),
            source_ref: 'kb://person/user-1/documents/doc-1',
            canonical_md_path: 'persons/user-1/kb/doc.md',
            status: 'ready',
            sensitivity: 'internal',
            agent_searchable: true,
            segment_count: 2,
            created_at: null,
            updated_at: null,
            metadata: {},
          },
        ]}
        selectedDocument={{
          document_id: 'doc-1',
          title: 'Taste notes',
          source_kind: 'paste',
          source_uri: 'clipboard://taste',
          source_sha256: 'a'.repeat(64),
          source_ref: 'kb://person/user-1/documents/doc-1',
          canonical_md_path: 'persons/user-1/kb/doc.md',
          status: 'ready',
          sensitivity: 'internal',
          agent_searchable: true,
          segment_count: 1,
          created_at: null,
          updated_at: null,
          metadata: {},
          segments: [
            {
              segment_id: 'seg-1',
              position: 0,
              heading_path: ['Retrieval'],
              content: 'Use source refs and ACL before context injection.',
              token_count: 8,
            },
          ],
        }}
        searchResults={[
          {
            document_id: 'doc-1',
            segment_id: 'seg-1',
            title: 'Taste notes',
            snippet: 'Use source refs and ACL before context injection.',
            source_ref: 'kb://person/user-1/documents/doc-1#segment=seg-1',
            score: 0.91,
            heading_path: ['Retrieval'],
            sensitivity: 'internal',
            metadata: {},
          },
        ]}
        selectedDocumentId="doc-1"
        searchQuery="source refs"
        onSearchQueryChange={() => {}}
        onRunSearch={() => {}}
        onSelectDocument={() => {}}
      />,
    );

    expect(html).toContain('Personal KB');
    expect(html).toContain('Read-only owner-scope view');
    expect(html).toContain('Taste notes');
    expect(html).toContain('Use source refs and ACL');
    expect(html).toContain('kb://person/user-1/documents/doc-1#segment=seg-1');
    expect(html).toContain('source refs');
    expect(html).not.toContain('Add to Personal KB');
    expect(html).not.toContain('Paste Markdown or notes here');
  });
});
