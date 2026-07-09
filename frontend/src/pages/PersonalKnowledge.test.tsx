import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

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
          metadata: { tags: ['CryptoAI', 'MEV'] },
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
  },
}));

import PersonalKnowledge from './PersonalKnowledge';

describe('PersonalKnowledge', () => {
  it('renders the owner-level Personal KB workbench with horizontal sections instead of a nested rail', () => {
    const html = renderToStaticMarkup(<PersonalKnowledge />);

    expect(html).toContain('个人知识库');
    expect(html).toContain('收集箱');
    expect(html).toContain('文库');
    expect(html).toContain('知识网');
    expect(html).toContain('画像');
    expect(html).toContain('授权');
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
    expect(html).not.toContain('/agents/agent-1/knowledge/personal');
  });
});
