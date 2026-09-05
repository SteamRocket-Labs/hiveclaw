import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/core';
import zh from '../i18n/zh.json';
import { translateFromCatalog } from '../test/i18nMock';

const queryState = vi.hoisted(() => ({ errors: {} as Record<string, unknown> }));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (
      key: string,
      fallbackOrOptions?: string | Record<string, unknown>,
      options?: Record<string, unknown>,
    ) => translateFromCatalog(zh, key, fallbackOrOptions, options),
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
              stage: 'indexed',
              status: 'degraded',
              artifact_hash: 'a'.repeat(64),
              error_message: null,
              attempt_count: 2,
              metadata: { source_filename: 'upload.pdf', media_kind: 'document' },
              created_at: '2026-07-01T00:00:00Z',
              updated_at: '2026-07-01T00:10:00Z',
              terminal: true,
              retryable: false,
              cancellable: false,
              error_code: null,
              max_attempts: 5,
              lifecycle_status: 'completed',
              result_status: 'degraded',
              cancelled_at: null,
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
              requester_user_id: 'user-1',
              session_id: null,
              purpose: 'autonomous_agent',
              delegation_id: null,
              sensitivity_ceiling: 'PL3_sensitive',
              binding_key: 'pkb:test',
              expires_at: '2099-01-01T00:00:00Z',
              revoked_at: null,
              revoked_by_user_id: null,
              active: true,
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

import PersonalKnowledge, {
  DocumentDetail,
  GraphPanel,
  GrantsPanel,
  ImportJobs,
  InboxPanel,
  ProposalReviewPanel,
  RevisionHistory,
  actionErrorCode,
} from './PersonalKnowledge';

describe('PersonalKnowledge', () => {
  beforeEach(() => {
    queryState.errors = {};
  });

  it('renders the owner-level Personal KB workbench with horizontal sections instead of a nested rail', () => {
    const html = renderToStaticMarkup(<PersonalKnowledge />);

    expect(html).toContain('个人知识库');
    expect(html).toContain('href="/knowledge/company"');
    expect(html).toContain('打开公司知识库');
    expect(html).toContain('收集箱');
    expect(html).toContain('文库');
    expect(html).toContain('知识网');
    expect(html).toContain('画像');
    expect(html).toContain('授权');
    expect(html).toContain('Agent 提案');
    expect(html).not.toContain('企业库（只读）');
    expect(html).not.toContain('/enterprise/memory');
    expect(html).toContain('+ 添加');
    expect(html).toContain('拖拽或选择文件');
    // Novice-facing copy: no pipeline/owner-level/canonical jargon on the
    // first screen or in lane helpers.
    expect(html).toContain('个人资料库');
    expect(html).not.toContain('Owner 级别的一份真相');
    expect(html).not.toContain('canonical MD');
    expect(html).not.toContain('taste / profile');
    expect(html).not.toContain('投喂与管线');
    expect(html).not.toContain('摄取管线');
    expect(html).toContain('PDF');
    expect(html).toContain('Word / DOCX');
    expect(html).toContain('md · txt');
    // Only the vertically proven formats are advertised — no media, CSV, HTML.
    expect(html).not.toContain('音频');
    expect(html).not.toContain('视频');
    expect(html).not.toContain('mp3 · wav');
    expect(html).not.toContain('mp4 · mov');
    expect(html).not.toContain('png · jpg');
    expect(html).not.toContain('csv · html');
    expect(html).toContain('URL 导入');
    expect(html).toContain('upload.pdf');
    // The degraded job renders a localized result label; the raw machine code
    // never enters the DOM.
    expect(html).toContain('部分索引');
    expect(html).not.toContain('knowledge_extraction_failed');
    expect(html).not.toContain('>degraded<');
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
    // The proposal's sensitivity renders as plain language, not the raw enum.
    expect(proposalHtml).toContain('公开');
    expect(proposalHtml).not.toContain('PL1_public');
    // Destination keeps the exact collection slug behind a plain label;
    // review state and ruling are localized, raw enum jargon is gone.
    expect(proposalHtml).toContain('保存到: operations');
    expect(proposalHtml).toContain('等待你审阅');
    expect(proposalHtml).toContain('需要你审阅');
    expect(proposalHtml).not.toContain('>ask<');
    expect(proposalHtml).not.toContain('>pending<');
    expect(revisionHtml).toContain('版本 1');
    expect(revisionHtml).toContain('Owner approved proposal');
    expect(revisionHtml).toContain('回滚到此版本');
  });

  it('renders graph counters and entity details in plain language with exact values kept', () => {
    const html = renderToStaticMarkup(
      <GraphPanel
        graph={{
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
        }}
      />,
    );

    // Counters use the page's own plain glosses, not schema nouns.
    expect(html).toContain('1 个人与主题');
    expect(html).toContain('0 个关联');
    expect(html).toContain('0 条陈述');
    // The free-form entity type value stays exact behind a plain label;
    // extraction confidence reads as a percentage.
    expect(html).toContain('类型: topic');
    expect(html).toContain('可信度: 92%');
    expect(html).not.toContain('entities');
    expect(html).not.toContain('assertions');
    expect(html).not.toContain('0.92');
  });

  it('renders every authority binding required for an autonomous or cross-principal grant', () => {
    const html = renderToStaticMarkup(
      <GrantsPanel
        grants={[]}
        granteeType="agent"
        granteeId="agent-1"
        permission="read"
        requesterUserId=""
        sessionId=""
        purpose="autonomous_agent"
        delegationId=""
        sensitivityCeiling="PL3_sensitive"
        expiresAt="2099-01-01T00:00"
        onGranteeTypeChange={vi.fn()}
        onGranteeIdChange={vi.fn()}
        onPermissionChange={vi.fn()}
        onRequesterUserIdChange={vi.fn()}
        onSessionIdChange={vi.fn()}
        onPurposeChange={vi.fn()}
        onDelegationIdChange={vi.fn()}
        onSensitivityCeilingChange={vi.fn()}
        onExpiresAtChange={vi.fn()}
        onCreate={vi.fn()}
        onDelete={vi.fn()}
        createPending={false}
        deletingGrantId={null}
      />,
    );

    // Exact enum values stay on the wire as option values…
    expect(html).toContain('value="autonomous_agent"');
    expect(html).toContain('value="interactive_session"');
    expect(html).toContain('value="a2a_delegation"');
    expect(html).toContain('value="PL3_sensitive"');
    expect(html).toContain('value="PL4_credential"');
    expect(html).toContain('datetime-local');
    expect(html).toContain('到期时间');
    expect(html).not.toContain('value="session"');
    // …while the visible option text is plain language.
    expect(html).toContain('>自主运行<');
    expect(html).toContain('>会话中协作<');
    expect(html).toContain('>转派给其他 Agent<');
    expect(html).toContain('>转派给子 Agent<');
    expect(html).toContain('>公开<');
    expect(html).toContain('>个人信息<');
    expect(html).toContain('>敏感<');
    expect(html).toContain('>凭据（仅引用）<');
    expect(html).toContain('>检索<');
    expect(html).toContain('>阅读<');
    expect(html).toContain('>管理<');
    expect(html).toContain('>其他人<');
    expect(html).not.toContain('>PL1_public<');
    expect(html).not.toContain('>PL2_pii<');
    expect(html).not.toContain('>PL3_sensitive<');
    expect(html).not.toContain('>PL4_credential<');
    expect(html).not.toContain('>autonomous_agent<');
    expect(html).not.toContain('>interactive_session<');
    expect(html).toContain('Agent 或用户 ID');
  });

  it('renders existing grants with plain-language labels while keeping exact identifiers visible', () => {
    const html = renderToStaticMarkup(
      <GrantsPanel
        grants={[
          {
            grant_id: 'grant-1',
            resource_type: 'scope',
            resource_id: 'user-1',
            document_id: null,
            grantee_type: 'agent',
            grantee_id: 'agent-1',
            permission: 'search',
            requester_user_id: 'user-9',
            session_id: 'session-42',
            purpose: 'interactive_session',
            delegation_id: 'delegation-7',
            sensitivity_ceiling: 'PL4_credential',
            binding_key: 'pkb:test',
            expires_at: '2099-01-01T00:00:00Z',
            revoked_at: null,
            revoked_by_user_id: null,
            active: true,
            metadata: {},
            created_at: '2026-07-01T00:00:00Z',
          },
          {
            grant_id: 'grant-2',
            resource_type: 'document',
            resource_id: 'doc-9',
            document_id: 'doc-9',
            grantee_type: 'user',
            grantee_id: 'user-2',
            permission: 'read',
            requester_user_id: null,
            session_id: null,
            purpose: null,
            delegation_id: null,
            sensitivity_ceiling: 'PL2_pii',
            binding_key: 'pkb:test2',
            expires_at: null,
            revoked_at: null,
            revoked_by_user_id: null,
            active: true,
            metadata: {},
            created_at: '2026-07-02T00:00:00Z',
          },
        ]}
        granteeType="agent"
        granteeId=""
        permission="search"
        requesterUserId=""
        sessionId=""
        purpose="autonomous_agent"
        delegationId=""
        sensitivityCeiling="PL3_sensitive"
        expiresAt="2099-01-01T00:00"
        onGranteeTypeChange={vi.fn()}
        onGranteeIdChange={vi.fn()}
        onPermissionChange={vi.fn()}
        onRequesterUserIdChange={vi.fn()}
        onSessionIdChange={vi.fn()}
        onPurposeChange={vi.fn()}
        onDelegationIdChange={vi.fn()}
        onSensitivityCeilingChange={vi.fn()}
        onExpiresAtChange={vi.fn()}
        onCreate={vi.fn()}
        onDelete={vi.fn()}
        createPending={false}
        deletingGrantId={null}
      />,
    );

    // Plain-language row labels…
    expect(html).toContain('Agent: agent-1');
    expect(html).toContain('其他人: user-2');
    expect(html).toContain('检索 · 整个资料库 · 凭据（仅引用） · 会话中协作');
    expect(html).toContain('阅读 · 单个文档 · 个人信息');
    expect(html).toContain('发起用户: user-9');
    expect(html).toContain('会话: session-42');
    expect(html).toContain('委派: delegation-7');
    // …with the exact identifiers retained, but no raw enum or snake_case label.
    expect(html).not.toContain('agent:agent-1');
    expect(html).not.toContain('user:user-2');
    expect(html).not.toContain('· scope ·');
    expect(html).not.toContain('· document ·');
    expect(html).not.toContain('PL4_credential · ');
    expect(html).not.toContain('>PL2_pii<');
    expect(html).not.toContain('requester:');
    expect(html).not.toContain('session:');
    expect(html).not.toContain('delegation:');
    // Grant sharing stays truthful: Agent grants expire, person grants do not
    // promise one.
    expect(html).toContain('给 Agent 的授权一定会到期；给人的授权在你撤销前一直有效');
  });

  it('renders a 403 as access denied and never as zero assets or an empty Personal KB', () => {
    queryState.errors['personal-knowledge-documents'] = new ApiError(403, 'Forbidden');

    const html = renderToStaticMarkup(<PersonalKnowledge />);

    expect(html).toContain('data-personal-knowledge-state="forbidden"');
    expect(html).toContain('这不是空知识库');
    expect(html).toContain('请让库主或管理员为你授权');
    expect(html).not.toContain('Owner scope');
    expect(html).not.toContain('0 文档');
    expect(html).not.toContain('个人知识库为空');
  });
});

// ---------------------------------------------------------------------------
// RC-01: import job lifecycle, actions, truthful status/error surfaces
// ---------------------------------------------------------------------------

type JobFixture = {
  job_id: string;
  document_id: string;
  stage: string;
  status: string;
  artifact_hash: string;
  error_message: string | null;
  attempt_count: number;
  metadata: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
  terminal: boolean;
  retryable: boolean;
  cancellable: boolean;
  error_code: string | null;
  max_attempts: number;
  lifecycle_status: string;
  result_status: string | null;
  cancelled_at: string | null;
};

function jobFixture(overrides: Partial<JobFixture>): JobFixture {
  return {
    job_id: 'job-x',
    document_id: 'doc-x',
    stage: 'queued',
    status: 'queued',
    artifact_hash: 'a'.repeat(64),
    error_message: null,
    attempt_count: 0,
    metadata: { source_filename: 'report.pdf' },
    created_at: null,
    updated_at: null,
    terminal: false,
    retryable: false,
    cancellable: true,
    error_code: null,
    max_attempts: 5,
    lifecycle_status: 'queued',
    result_status: null,
    cancelled_at: null,
    ...overrides,
  };
}

describe('PersonalKnowledge ImportJobs lifecycle surface', () => {
  it('shows a localized queued state with a truthful cancel action and no retry', () => {
    const html = renderToStaticMarkup(
      <ImportJobs jobs={[jobFixture({})]} onRetry={() => {}} onCancel={() => {}} />,
    );
    expect(html).toContain('排队中');
    expect(html).toContain('>取消<');
    expect(html).not.toContain('>重试<');
    expect(html).not.toContain('>queued<');
    expect(html).toContain('report.pdf');
  });

  it('shows a failed retryable job with retry action and localized typed error', () => {
    const html = renderToStaticMarkup(
      <ImportJobs
        jobs={[jobFixture({
          job_id: 'job-f',
          status: 'failed',
          lifecycle_status: 'failed',
          result_status: 'failed',
          terminal: true,
          retryable: true,
          cancellable: false,
          attempt_count: 1,
          error_code: 'conversion_timeout',
          error_message: 'conversion_timeout',
        })]}
        onRetry={() => {}}
        onCancel={() => {}}
      />,
    );
    expect(html).toContain('失败');
    expect(html).toContain('转换超时，可以重试。');
    expect(html).toContain('>重试<');
    expect(html).not.toContain('>取消<');
    expect(html).not.toContain('>conversion_timeout<');
  });

  it('maps unknown lifecycle and unknown error codes to one neutral localized label', () => {
    const html = renderToStaticMarkup(
      <ImportJobs
        jobs={[jobFixture({
          job_id: 'job-u',
          status: 'mystery_state',
          lifecycle_status: 'mystery_state',
          terminal: true,
          cancellable: false,
          error_code: 'totally_unknown_backend_code',
          error_message: 'totally_unknown_backend_code',
        })]}
        onRetry={() => {}}
        onCancel={() => {}}
      />,
    );
    expect(html).toContain('状态不可用');
    expect(html).toContain('导入失败，原因未知。');
    // Raw backend codes never enter the DOM, even in attributes.
    expect(html).not.toContain('mystery_state');
    expect(html).not.toContain('totally_unknown_backend_code');
  });

  it('shows cancelled jobs with the localized cancelled lifecycle and a truthful retry recovery', () => {
    // A cancelled job with attempts remaining is retryable (cancel recovery):
    // the read model exposes retryable and the UI offers the Retry action.
    const html = renderToStaticMarkup(
      <ImportJobs
        jobs={[jobFixture({
          job_id: 'job-c',
          status: 'cancelled',
          lifecycle_status: 'cancelled',
          result_status: 'cancelled',
          terminal: true,
          retryable: true,
          cancellable: false,
          cancelled_at: '2026-08-25T01:02:03+00:00',
        })]}
        onRetry={() => {}}
        onCancel={() => {}}
      />,
    );
    expect(html).toContain('已取消');
    expect(html).not.toContain('>cancelled<');
    expect(html).toContain('>重试<');
    expect(html).not.toContain('>取消<');
  });

  it('shows permanent failures without a retry action', () => {
    const html = renderToStaticMarkup(
      <ImportJobs
        jobs={[jobFixture({
          job_id: 'job-p',
          status: 'failed',
          lifecycle_status: 'failed',
          result_status: 'failed',
          terminal: true,
          retryable: false,
          cancellable: false,
          attempt_count: 1,
          error_code: 'unsupported_file_type',
        })]}
        onRetry={() => {}}
        onCancel={() => {}}
      />,
    );
    expect(html).toContain('失败');
    expect(html).toContain('不支持此文件类型。');
    // The novice guidance names the supported formats to switch to.
    expect(html).toContain('PDF');
    expect(html).toContain('纯文本');
    expect(html).not.toContain('>重试<');
    expect(html).not.toContain('unsupported_file_type');
  });

  it('explains media-failure prerequisites and keeps the retry action while the job stays retryable', () => {
    const html = renderToStaticMarkup(
      <ImportJobs
        jobs={[jobFixture({
          job_id: 'job-media',
          status: 'failed',
          lifecycle_status: 'failed',
          result_status: 'failed',
          terminal: true,
          retryable: true,
          cancellable: false,
          attempt_count: 1,
          error_code: 'unsupported_or_unconfigured',
          error_message: 'unsupported_or_unconfigured:media_transcription_provider',
          metadata: { source_filename: 'voice-memo.mp3', media_kind: 'audio' },
        })]}
        onRetry={() => {}}
        onCancel={() => {}}
      />,
    );
    expect(html).toContain('voice-memo.mp3');
    // The explanation names both prerequisites: a possibly missing
    // administrator-enabled capability and the supported-format alternative.
    expect(html).toContain('管理员');
    expect(html).toContain('纯文本');
    // Retryability still comes from the backend fact alone.
    expect(html).toContain('>重试<');
    expect(html).not.toContain('unsupported_or_unconfigured');
    expect(html).not.toContain('media_transcription_provider');
  });

  it('keeps a non-retryable media failure truthful: no retry action and no retry promise in the copy', () => {
    const html = renderToStaticMarkup(
      <ImportJobs
        jobs={[jobFixture({
          job_id: 'job-media-limit',
          status: 'failed',
          lifecycle_status: 'failed',
          result_status: 'failed',
          terminal: true,
          retryable: false,
          cancellable: false,
          attempt_count: 5,
          error_code: 'unsupported_or_unconfigured',
          metadata: { source_filename: 'voice-memo.mp3', media_kind: 'audio' },
        })]}
        onRetry={() => {}}
        onCancel={() => {}}
      />,
    );
    expect(html).toContain('voice-memo.mp3');
    expect(html).toContain('管理员');
    // No retry is offered (backend fact) and the explanation itself must not
    // promise one: the word 重试 never appears inside this row.
    expect(html).not.toContain('重试');
  });

  it('disables the in-flight job action while another job stays actionable', () => {
    const html = renderToStaticMarkup(
      <ImportJobs
        jobs={[
          jobFixture({ job_id: 'job-busy', metadata: { source_filename: 'busy.pdf' } }),
          jobFixture({ job_id: 'job-free', metadata: { source_filename: 'free.pdf' } }),
        ]}
        onRetry={() => {}}
        onCancel={() => {}}
        busyJobId="job-busy"
      />,
    );
    const rows = html.split('personal-kb-job-row').slice(1);
    expect(rows[0]).toContain('disabled=""');
    expect(rows[1]).not.toContain('disabled=""');
  });
});

describe('PersonalKnowledge document detail lifecycle actions', () => {
  const archivedDocument = {
    document_id: 'doc-archived',
    title: 'Archived research',
    source_kind: 'upload',
    source_uri: 'upload://archived.pdf',
    source_sha256: 'a'.repeat(64),
    source_ref: 'kb://person/user-1/documents/doc-archived',
    canonical_md_path: 'persons/user-1/kb/archived.md',
    status: 'archived',
    sensitivity: 'internal',
    agent_searchable: true,
    segment_count: 3,
    created_at: null,
    updated_at: null,
    metadata: {},
    segments: [],
  };

  it('offers restore for an archived document instead of archive', () => {
    const html = renderToStaticMarkup(
      <DocumentDetail
        document={archivedDocument}
        onRebuild={() => {}}
        onToggleAgentSearchable={() => {}}
        onArchive={() => {}}
        onRestore={() => {}}
        rebuildPending={false}
        patchPending={false}
        restorePending={false}
        revisions={[]}
        revisionsLoading={false}
        rollbackPendingVersion={null}
        onRollback={() => {}}
        onRetryRevisions={() => {}}
      />,
    );
    expect(html).toContain('已归档');
    expect(html).toContain('>恢复<');
    expect(html).not.toContain('>归档<');
  });

  it('offers archive for a consumable document instead of restore', () => {
    const html = renderToStaticMarkup(
      <DocumentDetail
        document={{ ...archivedDocument, status: 'ready' }}
        onRebuild={() => {}}
        onToggleAgentSearchable={() => {}}
        onArchive={() => {}}
        onRestore={() => {}}
        rebuildPending={false}
        patchPending={false}
        restorePending={false}
        revisions={[]}
        revisionsLoading={false}
        rollbackPendingVersion={null}
        onRollback={() => {}}
        onRetryRevisions={() => {}}
      />,
    );
    expect(html).toContain('已就绪');
    expect(html).toContain('>归档<');
    expect(html).not.toContain('>恢复<');
  });
});

describe('PersonalKnowledge action error visibility', () => {
  it('maps typed conflicts and untyped failures to bounded visible codes', () => {
    const conflict = new ApiError(409, 'Conflict', { code: 'retry_attempt_limit' });
    expect(actionErrorCode(conflict)).toBe('retry_attempt_limit');
    // Ordinary 404/500/network failures become the one bounded generic code —
    // the action error stays visible instead of silently disappearing.
    expect(actionErrorCode(new ApiError(500, 'Internal Server Error'))).toBe('unknown');
    expect(actionErrorCode(new ApiError(404, 'Not Found'))).toBe('unknown');
    expect(actionErrorCode(new Error('network down'))).toBe('unknown');
    expect(actionErrorCode(undefined)).toBe('unknown');
  });

  it('renders generic and typed action errors as localized alerts', () => {
    const generic = renderToStaticMarkup(
      <ImportJobs jobs={[jobFixture({})]} onRetry={() => {}} onCancel={() => {}} actionError="unknown" />,
    );
    expect(generic).toContain('role="alert"');
    expect(generic).toContain('操作未能完成');
    const typed = renderToStaticMarkup(
      <ImportJobs jobs={[jobFixture({})]} onRetry={() => {}} onCancel={() => {}} actionError="retry_attempt_limit" />,
    );
    expect(typed).toContain('已达重试次数上限');
    expect(typed).not.toContain('retry_attempt_limit');
  });

  it('does not duplicate the lifecycle label with an identical result label', () => {
    const failed = renderToStaticMarkup(
      <ImportJobs
        jobs={[jobFixture({
          job_id: 'job-dup-f',
          status: 'failed',
          lifecycle_status: 'failed',
          result_status: 'failed',
          terminal: true,
          retryable: true,
          cancellable: false,
        })]}
        onRetry={() => {}}
        onCancel={() => {}}
      />,
    );
    expect(failed.match(/失败/g)?.length).toBe(1);
    const cancelled = renderToStaticMarkup(
      <ImportJobs
        jobs={[jobFixture({
          job_id: 'job-dup-c',
          status: 'cancelled',
          lifecycle_status: 'cancelled',
          result_status: 'cancelled',
          terminal: true,
          cancellable: false,
        })]}
        onRetry={() => {}}
        onCancel={() => {}}
      />,
    );
    expect(cancelled.match(/已取消/g)?.length).toBe(1);
    // Completed keeps the informative result label.
    const completed = renderToStaticMarkup(
      <ImportJobs
        jobs={[jobFixture({
          job_id: 'job-dup-ok',
          status: 'degraded',
          lifecycle_status: 'completed',
          result_status: 'degraded',
          terminal: true,
          cancellable: false,
        })]}
        onRetry={() => {}}
        onCancel={() => {}}
      />,
    );
    expect(completed).toContain('已完成');
    expect(completed).toContain('部分索引');
  });
});

describe('PersonalKnowledge intake error surface', () => {
  const intakeProps = {
    title: '',
    markdown: '',
    url: '',
    selectedFile: null,
    jobs: [],
    jobsLoading: false,
    onTitleChange: () => {},
    onMarkdownChange: () => {},
    onUrlChange: () => {},
    onFileChange: () => {},
    onPasteSubmit: () => {},
    onFileSubmit: () => {},
    onUrlSubmit: () => {},
    onRetryJob: () => {},
    onCancelJob: () => {},
    onRetryJobsQuery: () => {},
    pastePending: false,
    filePending: false,
    urlPending: false,
  };

  it('renders a typed oversize upload rejection as a localized alert', () => {
    const html = renderToStaticMarkup(<InboxPanel {...intakeProps} intakeError="upload_too_large" />);
    expect(html).toContain('role="alert"');
    expect(html).toContain('文件过大，无法导入。');
    expect(html).not.toContain('upload_too_large');
  });

  it('renders any other intake failure as the generic localized alert', () => {
    const html = renderToStaticMarkup(<InboxPanel {...intakeProps} intakeError="unknown" />);
    expect(html).toContain('role="alert"');
    expect(html).toContain('操作未能完成');
  });

  it('renders no intake alert when there is no intake error', () => {
    const html = renderToStaticMarkup(<InboxPanel {...intakeProps} />);
    expect(html).not.toContain('role="alert"');
  });
});
