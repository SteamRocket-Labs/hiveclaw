import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { ApiError } from '../../api/core';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';

// 记忆 tab IA tests (two-plane world, memory spec v1.2):
// - overview renders per-plane counts + failure-mode lifecycle + pipeline health
// - the retired flat-T3 counters (Active/Superseded/Archived) never resurface
// - plane subviews replace the dead "entries" view; Raw stays manage-only
// - Personal KB is its own library lane, not mixed into agent Memory raw files

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
  memoryStatus: {
    state: 'consolidating',
    availableForRecall: true,
    recentMemoryAvailable: true,
    longTermMemoryAvailable: true,
    pendingConsolidation: true,
    pendingItems: 3,
    issueCount: 0,
  },
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
      coverage_total: 4,
      coverage_reviewed: 0,
      coverage_complete: false,
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

import AgentKnowledgeSection, { knowledgeEntryHeading, PersonalKnowledgeView } from './AgentKnowledgeSection';

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
    // Product-facing memory state is explicit without exposing lifecycle implementation names.
    expect(html).toContain('正在巩固');
    expect(html).toContain('近期经历已可供后续对话回忆');
    expect(html).toContain('可供对话回忆');
    expect(html).toContain('长期记忆');
    expect(html).toContain('正在整理 3 段经历');
    expect(html).not.toContain('T0→T2');
    expect(html).not.toContain('心跳消化');
    expect(html).not.toContain('Dream 固化');
    expect(html).not.toContain('技能蒸馏');
    // growth freshness surfaces
    expect(html).toContain('成长报告更新于');
    // pending soul approvals deep-link to the evolution tab
    expect(html).toContain('去审批');
    // the retired flat-T3 counters never resurface
    expect(html).not.toContain('Superseded');
    expect(html).not.toContain('Archived');
    expect(html).not.toContain('extractor');
    expect(html).toContain('agent-knowledge-memory-status--consolidating');
    // Default view is Overview, not a file browser.
    expect(html).not.toContain('raw markdown browser');
  });

  it('replaces the dead entries view with plane subviews', () => {
    const html = renderToStaticMarkup(
      <AgentKnowledgeSection agentId="agent-1" canEdit onNavigateTab={() => {}} />,
    );
    for (const view of ['记忆概览', '自我认知', '人际与领域', '个人知识库', '知识网络', '里程碑', '成长记录', '身份与记忆文件']) {
      expect(html).toContain(view);
    }
    expect(html).not.toContain('>overview</button>');
    expect(html).not.toContain('>timeline</button>');
    expect(html).not.toContain('>raw</button>');
    // the dead subview BUTTONS are gone (copy like 'Skill candidates' may remain)
    expect(html).not.toContain('>entries</button>');
    expect(html).not.toContain('>candidates</button>');
    expect(html).toContain('查看当前身份');
  });

  it('renders stalled memory as a business recovery state without pipeline internals', () => {
    const original = { ...overviewData.memoryStatus };
    Object.assign(overviewData.memoryStatus, {
      state: 'needs_attention',
      recentMemoryAvailable: false,
      pendingItems: 1,
      issueCount: 1,
    });
    try {
      const html = renderToStaticMarkup(
        <AgentKnowledgeSection agentId="agent-1" canEdit={false} onNavigateTab={() => {}} />,
      );
      expect(html).toContain('正在恢复');
      expect(html).toContain('部分近期经历暂时未整理完成');
      expect(html).toContain('1 段经历等待恢复');
      expect(html).toContain('agent-knowledge-memory-status--needs-attention');
      expect(html).not.toContain('t2_pipeline');
      expect(html).not.toContain('runtime_task_id');
    } finally {
      Object.assign(overviewData.memoryStatus, original);
    }
  });

  it('does not expose an unknown backend lifecycle state as employee-facing copy', () => {
    const original = { ...overviewData.memoryStatus };
    Object.assign(overviewData.memoryStatus, {
      state: 't2_recovery_pending',
      issueCount: 1,
    });
    try {
      const html = renderToStaticMarkup(
        <AgentKnowledgeSection agentId="agent-1" canEdit={false} onNavigateTab={() => {}} />,
      );
      expect(html).toContain('正在恢复');
      expect(html).not.toContain('t2_recovery_pending');
    } finally {
      Object.assign(overviewData.memoryStatus, original);
    }
  });

  it('hides the Raw advanced view for use-only access', () => {
    const html = renderToStaticMarkup(
      <AgentKnowledgeSection agentId="agent-1" canEdit={false} onNavigateTab={() => {}} />,
    );
    expect(html).toContain('记忆概览');
    expect(html).not.toContain('raw');
    expect(html).not.toContain('查看当前身份');
  });

  it('renders the Personal KB lane as readable content without internal references inside Agent Detail', () => {
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
        isLoading={false}
        selectedDocumentId="doc-1"
        searchQuery="source refs"
        onRetry={vi.fn()}
        onSearchQueryChange={() => {}}
        onRunSearch={() => {}}
        onSelectDocument={() => {}}
      />,
    );

    expect(html).toContain('个人知识库');
    expect(html).toContain('仅查看');
    expect(html).toContain('Taste notes');
    expect(html).toContain('Use source refs and ACL');
    expect(html).toContain('source refs');
    expect(html).toContain('内部资料');
    expect(html).toContain('可供检索');
    expect(html).toContain('第 1 段');
    expect(html).not.toContain('kb://person/');
    expect(html).not.toContain('persons/user-1/kb/doc.md');
    expect(html).not.toContain('>ready<');
    expect(html).not.toContain(' tok');
    expect(html).not.toContain('Add to Personal KB');
    expect(html).not.toContain('Paste Markdown or notes here');
  });

  it('never turns an Agent Personal KB 403 into an empty owner-scope library', () => {
    const html = renderToStaticMarkup(
      <PersonalKnowledgeView
        documents={[]}
        searchResults={[]}
        selectedDocumentId={null}
        searchQuery=""
        error={new ApiError(403, 'Forbidden')}
        isLoading={false}
        onRetry={vi.fn()}
        onSearchQueryChange={() => {}}
        onRunSearch={() => {}}
        onSelectDocument={() => {}}
      />,
    );

    expect(html).toContain('data-personal-knowledge-state="forbidden"');
    expect(html).toContain('这不是空知识库');
    expect(html).not.toContain('这里还没有可用的个人知识。');
  });

  it('uses user-readable content rather than an internal id for an unheaded memory entry', () => {
    expect(knowledgeEntryHeading({ ...selfEntry, content: '', preview: '先确认需求再执行' }, '未命名记忆')).toBe(
      '先确认需求再执行',
    );
    expect(knowledgeEntryHeading({ ...selfEntry, content: '', preview: '' }, '未命名记忆')).toBe('未命名记忆');
    expect(knowledgeEntryHeading({ ...selfEntry, content: '', preview: '' }, '未命名记忆')).not.toContain(
      selfEntry.id,
    );
  });
});
