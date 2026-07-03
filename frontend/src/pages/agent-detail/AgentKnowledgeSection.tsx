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
import './AgentKnowledgeSection.css';

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

const DISTILLER_STATE_FALLBACK: Record<string, string> = {
  active: 'Active',
  stale: 'Stale',
  never_ran: 'Never run',
};

const FAILURE_STATUS_STYLE: Record<string, { cls: string; fallback: string }> = {
  active: { cls: 'agent-knowledge-fm-active', fallback: 'active' },
  规避中: { cls: 'agent-knowledge-fm-mitigating', fallback: 'mitigating' },
  已根除: { cls: 'agent-knowledge-fm-resolved', fallback: 'resolved' },
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
    <div className="agent-knowledge-overview-grid">
      <div className="agent-knowledge-card">
        <h4 className="agent-knowledge-card-title">🧬 {t('agent.knowledge.identityCard', '身份 Identity')}</h4>
        <div className="agent-knowledge-card-body">
          <div>{t('agent.knowledge.soulSections', 'Soul sections')}: {overview.identity.sections}</div>
          <div>
            {t('agent.knowledge.pendingSoul', '待审批 soul 候选')}: {overview.identity.pendingSoulCandidates}
            {overview.identity.pendingSoulCandidates > 0 && onNavigateTab && (
              <button className="btn btn-sm agent-knowledge-inline-btn" onClick={() => onNavigateTab('evolution')}>
                {t('agent.knowledge.goApprove', '去审批')} →
              </button>
            )}
          </div>
        </div>
      </div>
      <div className="agent-knowledge-card agent-knowledge-card--clickable" onClick={() => onOpenSubView('self')}>
        <h4 className="agent-knowledge-card-title">🪞 {t('agent.knowledge.selfCard', '自我认知')}</h4>
        <div className="agent-knowledge-card-body">
          <div>{t('agent.knowledge.selfEntries', '条目')}: {overview.planes.self.entries}</div>
          <div className="agent-knowledge-row-gap">
            {t('agent.knowledge.failureModes', '失败模式')}:{' '}
            <span className="agent-knowledge-fm-active">{fm.active} active</span>
            {' · '}
            <span className="agent-knowledge-fm-mitigating">{fm.mitigating} {t('agent.knowledge.mitigating', '规避中')}</span>
            {' · '}
            <span className="agent-knowledge-fm-resolved">{fm.resolved} {t('agent.knowledge.resolved', '已根除')}</span>
          </div>
        </div>
      </div>
      <div className="agent-knowledge-card">
        <h4 className="agent-knowledge-card-title">🗺 {t('agent.knowledge.planesCard', '记忆版图')}</h4>
        <div className="agent-knowledge-card-body">
          <div className="agent-knowledge-plane-link" onClick={() => onOpenSubView('profiles')}>
            👥 {t('agent.knowledge.profilesPlane', '人际与领域')}: {overview.planes.profiles.entries}
          </div>
          <div className="agent-knowledge-plane-link" onClick={() => onOpenSubView('knowledge')}>
            📚 {t('agent.knowledge.knowledgePlane', '知识网络')}: {overview.planes.knowledge.pages}
          </div>
          <div className="agent-knowledge-plane-link" onClick={() => onOpenSubView('milestones')}>
            🏁 {t('agent.knowledge.milestonesPlane', '里程碑')}: {overview.planes.milestones.pages}
          </div>
          <div>📌 {t('agent.knowledge.explicitPlane', '主人指令记忆')}: {overview.planes.explicit.active}</div>
        </div>
      </div>
      <div className="agent-knowledge-card">
        <h4 className="agent-knowledge-card-title">🩺 {t('agent.knowledge.pipelineCard', '记忆管线')}</h4>
        <div className="agent-knowledge-card-body">
          {distillers.map((status) => (
            <div key={status.name}>
              {t(`agent.knowledge.distiller.${status.name}`, status.name)}:{' '}
              <span
                className={
                  status.state === 'active'
                    ? 'agent-knowledge-distiller-active'
                    : status.state === 'stale'
                      ? 'agent-knowledge-distiller-stale'
                      : 'agent-knowledge-distiller-never'
                }
              >
                {t(`agent.knowledge.distillerState.${status.state}`, DISTILLER_STATE_FALLBACK[status.state] ?? status.state)}
              </span>
            </div>
          ))}
          {overview.pipeline?.stalled && (
            <div className="agent-knowledge-pipeline-stalled">
              ⚠ {t('agent.knowledge.pipelineStalled', '消化停滞')} · {t('agent.knowledge.pendingPackages', '积压')}:{' '}
              {overview.pipeline.pendingPackages ?? 0}
            </div>
          )}
          {overview.growth?.generatedAt && (
            <div className="agent-knowledge-growth-updated">
              {t('agent.knowledge.growthUpdated', '成长报告更新于')} {overview.growth.generatedAt.slice(0, 16)}
            </div>
          )}
        </div>
      </div>
      <div className="agent-knowledge-card">
        <h4 className="agent-knowledge-card-title">🔗 {t('agent.knowledge.capabilitiesCard', '关联能力')}</h4>
        <div className="agent-knowledge-card-body">
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
  const statusStyle = FAILURE_STATUS_STYLE[status];
  return (
    <div className="agent-knowledge-card">
      <div className="agent-knowledge-entry-head">
        <strong className="agent-knowledge-entry-title">{entryHeading(entry)}</strong>
        {status && (
          <span className={`badge agent-knowledge-status-badge${statusStyle ? ` ${statusStyle.cls}` : ''}`}>
            {status}
          </span>
        )}
      </div>
      <div className="agent-knowledge-entry-preview">{entry.preview}</div>
      <div className="agent-knowledge-entry-meta">
        <code>{entry.id}</code> · {entry.file}
      </div>
    </div>
  );
}

function PlaneEntriesView({ entries, emptyText }: { entries: KnowledgeEntry[]; emptyText: string }) {
  if (!entries.length) {
    return <p className="agent-knowledge-empty">{emptyText}</p>;
  }
  return (
    <div className="agent-knowledge-entries-grid">
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
    <div className={`agent-knowledge-pages${selectedPageId ? ' is-split' : ''}`}>
      <div className="agent-knowledge-pages-list">
        {pages.map((page) => (
          <button
            key={page.id}
            className={`btn btn-sm agent-knowledge-page-btn ${selectedPageId === page.id ? 'btn-primary' : ''}`}
            onClick={() => onSelect(page.id)}
          >
            {page.kind === 'knowledge' ? '📖' : '🏁'} {page.title}
          </button>
        ))}
        {pages.length === 0 && <p className="agent-knowledge-empty">{emptyText}</p>}
      </div>
      {selectedPageId && pageDetail && (
        <div className="agent-knowledge-page-detail">
          <div className="agent-knowledge-card">
            <MarkdownRenderer content={pageDetail.markdown} />
          </div>
          {(pageDetail.links?.outgoing?.length > 0 || pageDetail.links?.incoming?.length > 0) && (
            <div className="agent-knowledge-card">
              <h4 className="agent-knowledge-card-title">🔗 {t('agent.knowledge.linkedPages', 'Linked pages')}</h4>
              <div className="agent-knowledge-links">
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
                        className="badge agent-knowledge-link-missing"
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
    <div className="agent-knowledge-root">
      <div className="agent-knowledge-subviews">
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
          <p className="agent-knowledge-loading">{t('common.loading', 'Loading…')}</p>
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
        <div className="agent-knowledge-timeline">
          {(eventsQuery.data?.events ?? []).map((event, index) => (
            <div key={`${event.at}-${index}`} className="agent-knowledge-card agent-knowledge-timeline-row">
              <span className="agent-knowledge-timeline-time">{event.at.slice(0, 16)}</span>
              <span className="badge agent-knowledge-mr-2">{event.kind}</span>
              <span className="agent-knowledge-mr-2">{event.outcome}</span>
              <span className="agent-knowledge-timeline-summary">{event.summary}</span>
            </div>
          ))}
          {eventsQuery.data && eventsQuery.data.events.length === 0 && (
            <p className="agent-knowledge-loading">{t('agent.knowledge.noEvents', 'No memory events recorded yet.')}</p>
          )}
        </div>
      )}

      {canEdit && subView === 'raw' && <AgentMindSection agentId={agentId} canEdit={canEdit} />}
    </div>
  );
}
