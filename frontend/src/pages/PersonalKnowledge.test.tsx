import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/core';

const queryState = vi.hoisted(() => ({ errors: {} as Record<string, unknown> }));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key,
  }),
}));

vi.mock('react-router-dom', () => ({
  Link: ({ to, children, className }: any) => (
    <a href={to} className={className}>
      {children}
    </a>
  ),
}));

vi.mock('@tanstack/react-query', () => ({
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
  useMutation: () => ({ mutate: vi.fn(), isPending: false }),
  useQuery: ({ queryKey }: { queryKey: unknown[] }) => {
    const key = String(queryKey[0]);
    if (queryState.errors[key]) {
      return {
        data: undefined,
        isLoading: false,
        isError: true,
        error: queryState.errors[key],
        refetch: vi.fn(),
      };
    }
    if (key === 'personal-knowledge-documents') {
      return {
        data: {
          documents: [
            {
              document_id: 'doc-1',
              title: 'Vitalik: Crypto x AI 应用的前景与挑战',
              source_kind: 'link',
              source_uri: 'https://example.com/vitalik-ai',
              source_sha256: 'a'.repeat(64),
              source_ref: 'kb://person/user-1/documents/doc-1',
              canonical_md_path: 'persons/user-1/kb/doc.md',
              status: 'ready',
              sensitivity: 'internal',
              agent_searchable: true,
              segment_count: 14,
              created_at: '2026-07-01T00:00:00Z',
              updated_at: null,
              metadata: { tags: ['CryptoAI', 'MEV'] },
            },
          ],
        },
        isLoading: false,
      };
    }
    if (key === 'personal-knowledge-document') {
      return {
        data: {
          document_id: 'doc-1',
          title: 'Vitalik: Crypto x AI 应用的前景与挑战',
          source_kind: 'link',
          source_uri: 'https://example.com/vitalik-ai',
          source_sha256: 'a'.repeat(64),
          source_ref: 'kb://person/user-1/documents/doc-1',
          canonical_md_path: 'persons/user-1/kb/doc.md',
          status: 'ready',
          sensitivity: 'internal',
          agent_searchable: true,
          segment_count: 14,
          created_at: '2026-07-01T00:00:00Z',
          updated_at: null,
          metadata: {
            tags: ['CryptoAI', 'MEV'],
            media_kind: 'image',
            source_filename: '112233.png',
            source_mime_type: 'image/png',
          },
          segments: [
            {
              segment_id: 'seg-1',
              position: 0,
              heading_path: ['预测市场'],
              content: '预测市场是 Crypto x AI 里信息聚合的样板。',
              token_count: 12,
            },
          ],
        },
        isLoading: false,
      };
    }
    if (key === 'personal-knowledge-search') {
      return {
        data: {
          results: [
            {
              document_id: 'doc-1',
              segment_id: 'seg-1',
              title: 'Vitalik: Crypto x AI 应用的前景与挑战',
              snippet: '预测市场是 Crypto x AI 里信息聚合的样板。',
              source_ref: 'kb://person/user-1/documents/doc-1#segment=seg-1',
              score: 0.91,
              heading_path: ['预测市场'],
              sensitivity: 'internal',
              metadata: {},
            },
          ],
        },
        isLoading: false,
      };
    }
    if (key === 'personal-knowledge-import-jobs') {
      return {
        data: {
          jobs: [
            {
              job_id: 'job-1',
              document_id: 'doc-1',
              stage: 'extracting',
              status: 'degraded',
              artifact_hash: 'a'.repeat(64),
              error_message: 'knowledge_extraction_failed',
              attempt_count: 2,
              metadata: { source_filename: 'upload.pdf', media_kind: 'document' },
              created_at: '2026-07-01T00:00:00Z',
              updated_at: '2026-07-01T00:10:00Z',
            },
          ],
        },
        isLoading: false,
      };
    }
    if (key === 'personal-knowledge-graph') {
      return {
        data: {
          entities: [
            {
              entity_id: 'entity-1',
              canonical_name: 'Crypto x AI',
              entity_type: 'topic',
              aliases: ['CryptoAI'],
              description: 'Intersection of crypto and AI.',
              confidence: 0.92,
              source_refs: [{ document_id: 'doc-1', segment_id: 'seg-1' }],
            },
          ],
          links: [],
          assertions: [],
        },
        isLoading: false,
      };
    }
    if (key === 'personal-knowledge-grants') {
      return {
        data: {
          grants: [
            {
              grant_id: 'grant-1',
              resource_type: 'scope',
              resource_id: 'user-1',
              document_id: null,
              grantee_type: 'agent',
              grantee_id: 'agent-1',
              permission: 'search',
              expires_at: null,
              metadata: { reason: 'research' },
              created_at: '2026-07-01T00:00:00Z',
            },
          ],
        },
        isLoading: false,
      };
    }
    if (key === 'personal-knowledge-proposals') {
      return {
        data: {
          proposals: [
            {
              proposal_id: 'proposal-1',
              owner_user_id: 'user-1',
              proposed_by_agent_id: 'agent-1',
              delegated_by_agent_id: null,
              delegation_id: null,
              title: 'Incident response',
              content: 'Escalate SEV-1 incidents immediately.',
              content_hash: 'c'.repeat(64),
              baseline_document_id: null,
              baseline_revision_id: null,
              baseline_content_hash: null,
              diff_unified: '--- current\n+++ proposed\n+Escalate SEV-1 incidents immediately.',
              target_collection: 'operations',
              source_refs: ['artifact://incident-42'],
              sensitivity: 'PL1_public',
              purpose: 'Preserve a verified operating rule.',
              dedupe_key: 'incident-response',
              idempotency_key: 'proposal-key',
              policy_outcome: 'ask',
              policy_reason_codes: [],
              status: 'pending',
              review_reason: null,
              document_id: null,
              revision_id: null,
              rollback_ref: null,
              created_at: '2026-07-01T00:00:00Z',
              updated_at: null,
            },
          ],
        },
        isLoading: false,
      };
    }
    if (key === 'personal-knowledge-revisions') {
      return {
        data: {
          revisions: [
            {
              id: 'revision-1',
              version: 1,
              change_source: 'agent_proposal',
              change_message: 'Owner approved proposal',
              created_at: '2026-07-01T00:00:00Z',
              content: { title: 'Incident response' },
            },
          ],
        },
        isLoading: false,
      };
    }
    return { data: undefined, isLoading: false };
  },
}));

vi.mock('../api/domains/knowledge', () => ({
  knowledgeApi: {
    myPersonalDocuments: vi.fn(),
    myPersonalDocument: vi.fn(),
    myPersonalSearch: vi.fn(),
    myPersonalIngest: vi.fn(),
    myPersonalImportFile: vi.fn(),
    myPersonalImportUrl: vi.fn(),
    myPersonalImportJobs: vi.fn(),
    myPersonalRetryImportJob: vi.fn(),
    myPersonalPatchDocument: vi.fn(),
    myPersonalRebuildDocument: vi.fn(),
    myPersonalGraph: vi.fn(),
    myPersonalGrants: vi.fn(),
    myPersonalCreateGrant: vi.fn(),
    myPersonalDeleteGrant: vi.fn(),
    myPersonalProposals: vi.fn(),
    myPersonalDecideProposal: vi.fn(),
    myPersonalDocumentRevisions: vi.fn(),
    myPersonalRollbackDocument: vi.fn(),
  },
}));

import PersonalKnowledge, { ProposalReviewPanel, RevisionHistory } from './PersonalKnowledge';

describe('PersonalKnowledge', () => {
  beforeEach(() => {
    queryState.errors = {};
  });

  it('renders the owner-level Personal KB workbench with horizontal sections instead of a nested rail', () => {
    const html = renderToStaticMarkup(<PersonalKnowledge />);

    expect(html).toContain('个人知识库');
    expect(html).toContain('收集箱');
    expect(html).toContain('文库');
    expect(html).toContain('知识网');
    expect(html).toContain('画像');
    expect(html).toContain('授权');
    expect(html).toContain('Agent 提案');
    expect(html).not.toContain('企业库（只读）');
    expect(html).not.toContain('/enterprise/memory');
    expect(html).toContain('+ 投喂');
    expect(html).toContain('拖拽或选择文件');
    expect(html).toContain('PDF');
    expect(html).toContain('Word / DOCX');
    expect(html).toContain('音频');
    expect(html).toContain('视频');
    expect(html).toContain('图片');
    expect(html).toContain('URL 导入');
    expect(html).toContain('upload.pdf');
    expect(html).toContain('knowledge_extraction_failed');
    expect(html).toContain('重建索引');
    expect(html).toContain('禁止 Agent 检索');
    expect(html).toContain('role="tablist"');
    expect(html).toContain('personal-kb-tabs');
    expect(html).not.toContain('personal-kb-rail');
    expect(html).not.toContain('上传和 URL 转换继续走后端统一摄取能力后再打开');
    expect(html).toContain('Vitalik: Crypto x AI 应用的前景与挑战');
    expect(html).toContain('kb://person/user-1/documents/doc-1');
    expect(html).toContain('预测市场是 Crypto x AI 里信息聚合的样板。');
    expect(html).toContain('源图片预览');
    expect(html).toContain('112233.png');
    expect(html).not.toContain('/agents/agent-1/knowledge/personal');
  });

  it('renders proposal evidence and reversible document history as owner-consumable control surfaces', () => {
    const proposalHtml = renderToStaticMarkup(
      <ProposalReviewPanel
        proposals={[
          {
            proposal_id: 'proposal-1',
            owner_user_id: 'user-1',
            proposed_by_agent_id: 'agent-1',
            delegated_by_agent_id: null,
            delegation_id: null,
            title: 'Incident response',
            content: 'Escalate SEV-1 incidents immediately.',
            content_hash: 'c'.repeat(64),
            baseline_document_id: null,
            baseline_revision_id: null,
            baseline_content_hash: null,
            diff_unified: '--- current\n+++ proposed\n+Escalate SEV-1 incidents immediately.',
            target_collection: 'operations',
            source_refs: ['artifact://incident-42'],
            sensitivity: 'PL1_public',
            purpose: 'Preserve a verified operating rule.',
            dedupe_key: 'incident-response',
            idempotency_key: 'proposal-key',
            policy_outcome: 'ask',
            policy_reason_codes: [],
            status: 'pending',
            review_reason: null,
            document_id: null,
            revision_id: null,
            rollback_ref: null,
            created_at: '2026-07-01T00:00:00Z',
            updated_at: null,
          },
        ]}
        busyProposalId={null}
        onDecision={vi.fn()}
      />,
    );
    const revisionHtml = renderToStaticMarkup(
      <RevisionHistory
        revisions={[
          {
            id: 'revision-2',
            version: 2,
            change_source: 'agent_proposal',
            change_message: 'Current version',
            created_at: '2026-07-02T00:00:00Z',
            content: { title: 'Incident response' },
          },
          {
            id: 'revision-1',
            version: 1,
            change_source: 'agent_proposal',
            change_message: 'Owner approved proposal',
            created_at: '2026-07-01T00:00:00Z',
            content: { title: 'Incident response' },
          },
        ]}
        busyVersion={null}
        onRollback={vi.fn()}
      />,
    );

    expect(proposalHtml).toContain('Escalate SEV-1 incidents immediately.');
    expect(proposalHtml).toContain('artifact://incident-42');
    expect(proposalHtml).toContain('批准并写入');
    expect(proposalHtml).toContain('拒绝');
    expect(revisionHtml).toContain('版本 1');
    expect(revisionHtml).toContain('Owner approved proposal');
    expect(revisionHtml).toContain('回滚到此版本');
  });

  it('renders a 403 as access denied and never as zero assets or an empty Personal KB', () => {
    queryState.errors['personal-knowledge-documents'] = new ApiError(403, 'Forbidden');

    const html = renderToStaticMarkup(<PersonalKnowledge />);

    expect(html).toContain('data-personal-knowledge-state="forbidden"');
    expect(html).toContain('This is not an empty knowledge base');
    expect(html).not.toContain('0 文档');
    expect(html).not.toContain('个人知识库为空');
  });
});
