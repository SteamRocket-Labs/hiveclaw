import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import AgentMindSection from './AgentMindSection';
import MarkdownRenderer from '../../components/MarkdownRenderer';
import {
  knowledgeApi,
  type KnowledgeCandidateItem,
  type KnowledgeOverview,
} from '../../api/domains/knowledge';

// Knowledge plane (spec §10): what the agent knows, where it came from,
// which memories are active, and which candidates await promotion.
// Capability modules stay independent — skill/workflow/MCP candidates only
// DEEP-LINK to their tabs, they are never managed here. The Raw subview is
// the advanced view over soul.md / memory/ / evolution/.

type SubView = 'overview' | 'pages' | 'entries' | 'candidates' | 'timeline' | 'raw';

const SUBVIEWS: SubView[] = ['overview', 'pages', 'entries', 'candidates', 'timeline', 'raw'];

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

function OverviewCards({ overview }: { overview: KnowledgeOverview }) {
  const { t } = useTranslation();
  const distillers = Object.values(overview.distillers ?? {});
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: '12px' }}>
      <div style={cardStyle}>
        <h4 style={{ marginBottom: '8px' }}>🧬 {t('agent.knowledge.identityCard', 'Identity')}</h4>
        <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
          <div>{t('agent.knowledge.soulSections', 'Soul sections')}: {overview.identity.sections}</div>
          <div>{t('agent.knowledge.pendingSoul', 'Pending soul candidates')}: {overview.identity.pendingSoulCandidates}</div>
        </div>
      </div>
      <div style={cardStyle}>
        <h4 style={{ marginBottom: '8px' }}>🧠 {t('agent.knowledge.memoryCard', 'Memory')}</h4>
        <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
          <div>{t('agent.knowledge.activeEntries', 'Active')}: {overview.memory.active}</div>
          <div>{t('agent.knowledge.superseded', 'Superseded')}: {overview.memory.superseded}</div>
          <div>{t('agent.knowledge.archived', 'Archived')}: {overview.memory.archived}</div>
        </div>
      </div>
      <div style={cardStyle}>
        <h4 style={{ marginBottom: '8px' }}>⚗️ {t('agent.knowledge.distillersCard', 'Distillers')}</h4>
        <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
          {distillers.map((status) => (
            <div key={status.name}>
              {status.name}: <span style={{ color: status.state === 'active' ? '#22c55e' : 'var(--text-tertiary)' }}>{status.state}</span>
            </div>
          ))}
        </div>
      </div>
      <div style={cardStyle}>
        <h4 style={{ marginBottom: '8px' }}>🔗 {t('agent.knowledge.capabilitiesCard', 'Linked capabilities')}</h4>
        <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
          <div>{t('agent.knowledge.skillsLinked', 'Skills')}: {overview.linkedCapabilities.skillsReferenced}</div>
          <div>{t('agent.knowledge.skillCandidates', 'Skill candidates')}: {overview.linkedCapabilities.skillCandidates}</div>
          <div>{t('agent.knowledge.workflowCandidates', 'Workflow candidates')}: {overview.linkedCapabilities.workflowsReferenced}</div>
        </div>
      </div>
    </div>
  );
}

function CandidateList({
  title,
  items,
  linkLabel,
  onLink,
}: {
  title: string;
  items: KnowledgeCandidateItem[];
  linkLabel: string;
  onLink?: () => void;
}) {
  if (!items.length) return null;
  return (
    <div style={cardStyle}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
        <h4>{title}</h4>
        {onLink && (
          <button className="btn btn-sm" onClick={onLink}>
            {linkLabel} →
          </button>
        )}
      </div>
      <ul style={{ fontSize: '13px', color: 'var(--text-secondary)', paddingLeft: '18px' }}>
        {items.map((item) => (
          <li key={item.entry_id} style={{ marginBottom: '4px' }}>
            <code style={{ fontSize: '11px' }}>{item.entry_id}</code> {item.content}
          </li>
        ))}
      </ul>
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

  const overviewQuery = useQuery({
    queryKey: ['knowledge-overview', agentId],
    queryFn: () => knowledgeApi.overview(agentId),
    enabled: subView === 'overview',
  });
  const pagesQuery = useQuery({
    queryKey: ['knowledge-pages', agentId],
    queryFn: () => knowledgeApi.pages(agentId),
    enabled: subView === 'pages',
  });
  const pageQuery = useQuery({
    queryKey: ['knowledge-page', agentId, selectedPageId],
    queryFn: () => knowledgeApi.page(agentId, selectedPageId as string),
    enabled: subView === 'pages' && !!selectedPageId,
  });
  const entriesQuery = useQuery({
    queryKey: ['knowledge-entries', agentId],
    queryFn: () => knowledgeApi.entries(agentId),
    enabled: subView === 'entries',
  });
  const eventsQuery = useQuery({
    queryKey: ['knowledge-events', agentId],
    queryFn: () => knowledgeApi.events(agentId),
    enabled: subView === 'timeline',
  });
  const candidatesQuery = useQuery({
    queryKey: ['knowledge-candidates', agentId],
    queryFn: () => knowledgeApi.candidates(agentId),
    enabled: subView === 'candidates' || subView === 'overview',
  });

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

      {subView === 'overview' && (
        <>
          {overviewQuery.data ? (
            <OverviewCards overview={overviewQuery.data} />
          ) : (
            <p style={{ color: 'var(--text-tertiary)' }}>{t('common.loading', 'Loading…')}</p>
          )}
          {candidatesQuery.data && candidatesQuery.data.heldCurations.length > 0 && (
            <div style={cardStyle}>
              <h4 style={{ marginBottom: '8px' }}>⏸️ {t('agent.knowledge.heldCurations', 'Held curation decisions')}</h4>
              <ul style={{ fontSize: '13px', color: 'var(--text-secondary)', paddingLeft: '18px' }}>
                {candidatesQuery.data.heldCurations.slice(0, 5).map((held, index) => (
                  <li key={`${held.at}-${index}`}>
                    [{held.stage}] {held.reason}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}

      {subView === 'pages' && (
        <div style={{ display: 'grid', gridTemplateColumns: selectedPageId ? '260px 1fr' : '1fr', gap: '16px' }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {(pagesQuery.data?.pages ?? []).map((page) => (
              <button
                key={page.id}
                className={`btn btn-sm ${selectedPageId === page.id ? 'btn-primary' : ''}`}
                style={{ justifyContent: 'flex-start', textAlign: 'left' }}
                onClick={() => setSelectedPageId(page.id)}
              >
                {page.kind === 'wiki' ? '📖' : '🎬'} {page.title}
              </button>
            ))}
            {pagesQuery.data && pagesQuery.data.pages.length === 0 && (
              <p style={{ color: 'var(--text-tertiary)', fontSize: '13px' }}>
                {t('agent.knowledge.noPages', 'No wiki or scene pages yet — the curators build them as evidence accumulates.')}
              </p>
            )}
          </div>
          {selectedPageId && pageQuery.data && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
              <div style={cardStyle}>
                <MarkdownRenderer content={pageQuery.data.markdown} />
              </div>
              {(pageQuery.data.links?.outgoing?.length > 0 || pageQuery.data.links?.incoming?.length > 0) && (
                <div style={cardStyle}>
                  <h4 style={{ marginBottom: '8px' }}>🔗 {t('agent.knowledge.linkedPages', 'Linked pages')}</h4>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
                    {[...(pageQuery.data.links?.outgoing ?? []), ...(pageQuery.data.links?.incoming ?? [])].map(
                      (link, index) =>
                        link.exists ? (
                          <button
                            key={`${link.page_id}-${index}`}
                            className="btn btn-sm"
                            onClick={() => setSelectedPageId(link.page_id)}
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
      )}

      {subView === 'entries' && (
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', fontSize: '13px', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ textAlign: 'left', color: 'var(--text-tertiary)' }}>
                <th style={{ padding: '6px' }}>{t('agent.knowledge.entryCategory', 'Category')}</th>
                <th style={{ padding: '6px' }}>{t('agent.knowledge.entryContent', 'Content')}</th>
                <th style={{ padding: '6px' }}>🔥 {t('agent.knowledge.entryHeat', 'Heat')}</th>
                <th style={{ padding: '6px' }}>{t('agent.knowledge.entryRecalls', 'Recalls')}</th>
                <th style={{ padding: '6px' }}>{t('agent.knowledge.entryLane', 'Lane')}</th>
              </tr>
            </thead>
            <tbody>
              {(entriesQuery.data?.entries ?? []).map((entry) => (
                <tr key={entry.id} style={{ borderTop: '1px solid var(--border-primary)' }}>
                  <td style={{ padding: '6px' }}>
                    <span className="badge">{entry.category}</span>
                  </td>
                  <td style={{ padding: '6px' }}>{entry.preview}</td>
                  <td style={{ padding: '6px' }}>{entry.heat}</td>
                  <td style={{ padding: '6px' }}>{entry.recallCount}</td>
                  <td style={{ padding: '6px', color: 'var(--text-tertiary)' }}>
                    {entry.promotedTo
                      ? `→ ${entry.promotedTo}`
                      : entry.containerCandidate || '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {subView === 'candidates' && candidatesQuery.data && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
          <CandidateList
            title={`🛠 ${t('agent.knowledge.skillCandidatesTitle', 'Skill candidates')}`}
            items={candidatesQuery.data.skillCandidates}
            linkLabel={t('agent.knowledge.openSkills', 'Open Skills')}
            onLink={onNavigateTab ? () => onNavigateTab('skills') : undefined}
          />
          <CandidateList
            title={`🧭 ${t('agent.knowledge.workflowCandidatesTitle', 'Workflow candidates')}`}
            items={candidatesQuery.data.workflowCandidates}
            linkLabel={t('agent.knowledge.openWorkflows', 'Open Workflows')}
            onLink={onNavigateTab ? () => onNavigateTab('workflows') : undefined}
          />
          {candidatesQuery.data.soulCandidates.length > 0 && (
            <div style={cardStyle}>
              <h4 style={{ marginBottom: '8px' }}>🧬 {t('agent.knowledge.soulCandidatesTitle', 'Soul candidates (held)')}</h4>
              <ul style={{ fontSize: '13px', color: 'var(--text-secondary)', paddingLeft: '18px' }}>
                {candidatesQuery.data.soulCandidates.map((candidate) => (
                  <li key={candidate.candidateId}>
                    <code style={{ fontSize: '11px' }}>{candidate.candidateId}</code> {candidate.reason}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {candidatesQuery.data.skillCandidates.length === 0 &&
            candidatesQuery.data.workflowCandidates.length === 0 &&
            candidatesQuery.data.soulCandidates.length === 0 && (
              <p style={{ color: 'var(--text-tertiary)' }}>
                {t('agent.knowledge.noCandidates', 'No open promotion candidates.')}
              </p>
            )}
        </div>
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
