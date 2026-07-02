import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import AgentMindSection from './AgentMindSection';
import MarkdownRenderer from '../../components/MarkdownRenderer';
import {
  knowledgeApi,
  type KnowledgeEntry,
  type KnowledgeOverview,
  type KnowledgePageSummary,
} from '../../api/domains/knowledge';

// 记忆 tab — the two-plane world (memory spec v1.2):
//   profile plane converges (self 自我认知 / profiles 人际与领域),
//   knowledge plane networks (knowledge 知识网络 / milestones 里程碑).
// First screen answers "这个员工记住了什么、最近学到什么、管线健康吗".
// Raw stays the admin escape hatch over soul.md / memory/.

type SubView = 'overview' | 'self' | 'profiles' | 'knowledge' | 'milestones' | 'timeline' | 'raw';

const SUBVIEWS: SubView[] = ['overview', 'self', 'profiles', 'knowledge', 'milestones', 'timeline', 'raw'];

type AgentKnowledgeSectionProps = {
  agentId: string;
  canEdit: boolean;
  onNavigateTab?: (tab: string) => void;
};

const cardStyle: React.CSSProperties = {
  border: '1px solid var(--border-primary)',
  borderRadius: '8px',
  padding: '12px 16px',
  background: 'var(--bg-secondary)',
};

const DISTILLER_STATE_FALLBACK: Record<string, string> = {
  active: 'Active',
  stale: 'Stale',
  never_ran: 'Never run',
};

const FAILURE_STATUS_STYLE: Record<string, { color: string; fallback: string }> = {
  active: { color: '#ef4444', fallback: 'active' },
  规避中: { color: '#f59e0b', fallback: 'mitigating' },
  已根除: { color: '#22c55e', fallback: 'resolved' },
};

function entryHeading(entry: KnowledgeEntry): string {
  const firstLine = (entry.content || '').split('\n').find((line) => line.startsWith('### '));
  return firstLine ? firstLine.replace(/^###\s+/, '') : entry.id;
}

function entryStatusLine(entry: KnowledgeEntry): string {
  const line = (entry.content || '').split('\n').find((raw) => raw.trim().startsWith('- 状态:'));
  return line ? line.trim().replace(/^- 状态:\s*/, '') : '';
}

function OverviewCards({
  overview,
  onOpenSubView,
  onNavigateTab,
}: {
  overview: KnowledgeOverview;
  onOpenSubView: (view: SubView) => void;
  onNavigateTab?: (tab: string) => void;
}) {
  const { t } = useTranslation();
  const distillers = Object.values(overview.distillers ?? {});
  const fm = overview.planes.self.failureModes;
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: '12px' }}>
      <div style={cardStyle}>
        <h4 style={{ marginBottom: '8px' }}>🧬 {t('agent.knowledge.identityCard', '身份 Identity')}</h4>
        <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
          <div>{t('agent.knowledge.soulSections', 'Soul sections')}: {overview.identity.sections}</div>
          <div>
            {t('agent.knowledge.pendingSoul', '待审批 soul 候选')}: {overview.identity.pendingSoulCandidates}
            {overview.identity.pendingSoulCandidates > 0 && onNavigateTab && (
              <button className="btn btn-sm" style={{ marginLeft: '8px' }} onClick={() => onNavigateTab('evolution')}>
                {t('agent.knowledge.goApprove', '去审批')} →
              </button>
            )}
          </div>
        </div>
      </div>
      <div style={{ ...cardStyle, cursor: 'pointer' }} onClick={() => onOpenSubView('self')}>
        <h4 style={{ marginBottom: '8px' }}>🪞 {t('agent.knowledge.selfCard', '自我认知')}</h4>
        <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
          <div>{t('agent.knowledge.selfEntries', '条目')}: {overview.planes.self.entries}</div>
          <div style={{ marginTop: '4px' }}>
            {t('agent.knowledge.failureModes', '失败模式')}:{' '}
            <span style={{ color: FAILURE_STATUS_STYLE.active.color }}>{fm.active} active</span>
            {' · '}
            <span style={{ color: FAILURE_STATUS_STYLE['规避中'].color }}>{fm.mitigating} {t('agent.knowledge.mitigating', '规避中')}</span>
            {' · '}
            <span style={{ color: FAILURE_STATUS_STYLE['已根除'].color }}>{fm.resolved} {t('agent.knowledge.resolved', '已根除')}</span>
          </div>
        </div>
      </div>
      <div style={cardStyle}>
        <h4 style={{ marginBottom: '8px' }}>🗺 {t('agent.knowledge.planesCard', '记忆版图')}</h4>
        <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
          <div style={{ cursor: 'pointer' }} onClick={() => onOpenSubView('profiles')}>
            👥 {t('agent.knowledge.profilesPlane', '人际与领域')}: {overview.planes.profiles.entries}
          </div>
          <div style={{ cursor: 'pointer' }} onClick={() => onOpenSubView('knowledge')}>
            📚 {t('agent.knowledge.knowledgePlane', '知识网络')}: {overview.planes.knowledge.pages}
          </div>
          <div style={{ cursor: 'pointer' }} onClick={() => onOpenSubView('milestones')}>
            🏁 {t('agent.knowledge.milestonesPlane', '里程碑')}: {overview.planes.milestones.pages}
          </div>
          <div>📌 {t('agent.knowledge.explicitPlane', '主人指令记忆')}: {overview.planes.explicit.active}</div>
        </div>
      </div>
      <div style={cardStyle}>
        <h4 style={{ marginBottom: '8px' }}>🩺 {t('agent.knowledge.pipelineCard', '记忆管线')}</h4>
        <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
          {distillers.map((status) => (
            <div key={status.name}>
              {t(`agent.knowledge.distiller.${status.name}`, status.name)}:{' '}
              <span
                style={{
                  color:
                    status.state === 'active'
                      ? '#22c55e'
                      : status.state === 'stale'
                        ? '#f59e0b'
                        : 'var(--text-tertiary)',
                }}
              >
                {t(`agent.knowledge.distillerState.${status.state}`, DISTILLER_STATE_FALLBACK[status.state] ?? status.state)}
              </span>
            </div>
          ))}
          {overview.pipeline?.stalled && (
            <div style={{ color: '#ef4444', marginTop: '4px' }}>
              ⚠ {t('agent.knowledge.pipelineStalled', '消化停滞')} · {t('agent.knowledge.pendingPackages', '积压')}:{' '}
              {overview.pipeline.pendingPackages ?? 0}
            </div>
          )}
          {overview.growth?.generatedAt && (
            <div style={{ color: 'var(--text-tertiary)', marginTop: '4px' }}>
              {t('agent.knowledge.growthUpdated', '成长报告更新于')} {overview.growth.generatedAt.slice(0, 16)}
            </div>
          )}
        </div>
      </div>
      <div style={cardStyle}>
        <h4 style={{ marginBottom: '8px' }}>🔗 {t('agent.knowledge.capabilitiesCard', '关联能力')}</h4>
        <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
          <div>{t('agent.knowledge.skillsLinked', 'Skills')}: {overview.linkedCapabilities.skillsReferenced}</div>
          <div>{t('agent.knowledge.skillCandidates', 'Skill candidates')}: {overview.linkedCapabilities.skillCandidates}</div>
          <div>{t('agent.knowledge.workflowCandidates', 'Workflow candidates')}: {overview.linkedCapabilities.workflowsReferenced}</div>
        </div>
      </div>
    </div>
  );
}

function ProfileEntryCard({ entry }: { entry: KnowledgeEntry }) {
  const status = entryStatusLine(entry);
  const style = FAILURE_STATUS_STYLE[status];
  return (
    <div style={cardStyle}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: '12px', marginBottom: '6px' }}>
        <strong style={{ fontSize: '13px' }}>{entryHeading(entry)}</strong>
        {status && (
          <span
            className="badge"
            style={{ color: style?.color ?? 'var(--text-tertiary)', whiteSpace: 'nowrap' }}
          >
            {status}
          </span>
        )}
      </div>
      <div style={{ fontSize: '12px', color: 'var(--text-secondary)', whiteSpace: 'pre-wrap' }}>{entry.preview}</div>
      <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '6px' }}>
        <code>{entry.id}</code> · {entry.file}
      </div>
    </div>
  );
}

function PlaneEntriesView({ entries, emptyText }: { entries: KnowledgeEntry[]; emptyText: string }) {
  if (!entries.length) {
    return <p style={{ color: 'var(--text-tertiary)', fontSize: '13px' }}>{emptyText}</p>;
  }
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))', gap: '12px' }}>
      {entries.map((entry) => (
        <ProfileEntryCard key={entry.id} entry={entry} />
      ))}
    </div>
  );
}

function PagesView({
  pages,
  selectedPageId,
  onSelect,
  pageDetail,
  emptyText,
}: {
  pages: KnowledgePageSummary[];
  selectedPageId: string | null;
  onSelect: (id: string) => void;
  pageDetail: ReturnType<typeof useQuery<any>>['data'];
  emptyText: string;
}) {
  const { t } = useTranslation();
  return (
    <div style={{ display: 'grid', gridTemplateColumns: selectedPageId ? '260px 1fr' : '1fr', gap: '16px' }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
        {pages.map((page) => (
          <button
            key={page.id}
            className={`btn btn-sm ${selectedPageId === page.id ? 'btn-primary' : ''}`}
            style={{ justifyContent: 'flex-start', textAlign: 'left' }}
            onClick={() => onSelect(page.id)}
          >
            {page.kind === 'knowledge' ? '📖' : '🏁'} {page.title}
          </button>
        ))}
        {pages.length === 0 && <p style={{ color: 'var(--text-tertiary)', fontSize: '13px' }}>{emptyText}</p>}
      </div>
      {selectedPageId && pageDetail && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
          <div style={cardStyle}>
            <MarkdownRenderer content={pageDetail.markdown} />
          </div>
          {(pageDetail.links?.outgoing?.length > 0 || pageDetail.links?.incoming?.length > 0) && (
            <div style={cardStyle}>
              <h4 style={{ marginBottom: '8px' }}>🔗 {t('agent.knowledge.linkedPages', 'Linked pages')}</h4>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
                {[...(pageDetail.links?.outgoing ?? []), ...(pageDetail.links?.incoming ?? [])].map(
                  (link: { page_id: string; title: string; rel_type: string; exists: boolean }, index: number) =>
                    link.exists ? (
                      <button
                        key={`${link.page_id}-${index}`}
                        className="btn btn-sm"
                        onClick={() => onSelect(link.page_id)}
                      >
                        {link.rel_type} → {link.title}
                      </button>
                    ) : (
                      <span
                        key={`${link.page_id}-${index}`}
                        className="badge"
                        style={{ color: 'var(--text-tertiary)' }}
                        title={t('agent.knowledge.pageNotCreated', 'Page not created yet')}
                      >
                        {link.rel_type} → {link.title} ✦
                      </span>
                    ),
                )}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function AgentKnowledgeSection({ agentId, canEdit, onNavigateTab }: AgentKnowledgeSectionProps) {
  const { t } = useTranslation();
  const [subView, setSubView] = useState<SubView>('overview');
  const [selectedPageId, setSelectedPageId] = useState<string | null>(null);
  const availableSubViews = canEdit ? SUBVIEWS : SUBVIEWS.filter((view) => view !== 'raw');

  useEffect(() => {
    if (!canEdit && subView === 'raw') {
      setSubView('overview');
    }
  }, [canEdit, subView]);

  useEffect(() => {
    setSelectedPageId(null);
  }, [subView]);

  const overviewQuery = useQuery({
    queryKey: ['knowledge-overview', agentId],
    queryFn: () => knowledgeApi.overview(agentId),
    enabled: subView === 'overview',
  });
  const entriesQuery = useQuery({
    queryKey: ['knowledge-entries', agentId],
    queryFn: () => knowledgeApi.entries(agentId),
    enabled: subView === 'self' || subView === 'profiles',
  });
  const pagesQuery = useQuery({
    queryKey: ['knowledge-pages', agentId],
    queryFn: () => knowledgeApi.pages(agentId),
    enabled: subView === 'knowledge' || subView === 'milestones',
  });
  const pageQuery = useQuery({
    queryKey: ['knowledge-page', agentId, selectedPageId],
    queryFn: () => knowledgeApi.page(agentId, selectedPageId as string),
    enabled: (subView === 'knowledge' || subView === 'milestones') && !!selectedPageId,
  });
  const eventsQuery = useQuery({
    queryKey: ['knowledge-events', agentId],
    queryFn: () => knowledgeApi.events(agentId),
    enabled: subView === 'timeline',
  });

  const allEntries = entriesQuery.data?.entries ?? [];
  const selfEntries = allEntries.filter((entry) => entry.file.endsWith('self/self.md'));
  const profileEntries = allEntries.filter((entry) => entry.file.includes('memory/profiles/'));
  const allPages = pagesQuery.data?.pages ?? [];
  const knowledgePages = allPages.filter((page) => page.kind === 'knowledge');
  const milestonePages = allPages.filter((page) => page.kind === 'milestone');

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
        {availableSubViews.map((view) => (
          <button
            key={view}
            className={`btn btn-sm ${subView === view ? 'btn-primary' : ''}`}
            onClick={() => setSubView(view)}
          >
            {t(`agent.knowledge.subview.${view}`, view)}
          </button>
        ))}
      </div>

      {subView === 'overview' &&
        (overviewQuery.data ? (
          <OverviewCards overview={overviewQuery.data} onOpenSubView={setSubView} onNavigateTab={onNavigateTab} />
        ) : (
          <p style={{ color: 'var(--text-tertiary)' }}>{t('common.loading', 'Loading…')}</p>
        ))}

      {subView === 'self' && (
        <PlaneEntriesView
          entries={selfEntries}
          emptyText={t(
            'agent.knowledge.noSelf',
            '还没有自我认知条目 — 能力、方法和失败模式会随经历由记忆管线长出来。',
          )}
        />
      )}

      {subView === 'profiles' && (
        <PlaneEntriesView
          entries={profileEntries}
          emptyText={t('agent.knowledge.noProfiles', '还没有主人/协作者/领域侧写条目。')}
        />
      )}

      {subView === 'knowledge' && (
        <PagesView
          pages={knowledgePages}
          selectedPageId={selectedPageId}
          onSelect={setSelectedPageId}
          pageDetail={pageQuery.data}
          emptyText={t('agent.knowledge.noPages', '还没有知识页 — 证据积累后由整理器生成。')}
        />
      )}

      {subView === 'milestones' && (
        <PagesView
          pages={milestonePages}
          selectedPageId={selectedPageId}
          onSelect={setSelectedPageId}
          pageDetail={pageQuery.data}
          emptyText={t('agent.knowledge.noMilestones', '还没有里程碑 — 首次成功、重大失败或主人反馈会沉淀在这里。')}
        />
      )}

      {subView === 'timeline' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
          {(eventsQuery.data?.events ?? []).map((event, index) => (
            <div key={`${event.at}-${index}`} style={{ ...cardStyle, padding: '8px 12px', fontSize: '13px' }}>
              <span style={{ color: 'var(--text-tertiary)', marginRight: '8px' }}>{event.at.slice(0, 16)}</span>
              <span className="badge" style={{ marginRight: '8px' }}>{event.kind}</span>
              <span style={{ marginRight: '8px' }}>{event.outcome}</span>
              <span style={{ color: 'var(--text-secondary)' }}>{event.summary}</span>
            </div>
          ))}
          {eventsQuery.data && eventsQuery.data.events.length === 0 && (
            <p style={{ color: 'var(--text-tertiary)' }}>{t('agent.knowledge.noEvents', 'No memory events recorded yet.')}</p>
          )}
        </div>
      )}

      {canEdit && subView === 'raw' && <AgentMindSection agentId={agentId} canEdit={canEdit} />}
    </div>
  );
}
