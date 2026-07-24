import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import {
  IconActivityHeartbeat,
  IconArchive,
  IconBulb,
  IconChartLine,
  IconHistory,
  IconShieldCheck,
  IconSnowflake,
  IconSparkles,
} from '@tabler/icons-react';

import { evolutionApi, type EvolutionManifest, type EvolutionTimelineItem } from '../../api/domains/evolution';
import { knowledgeApi, type GrowthMetrics } from '../../api/domains/knowledge';
import './AgentEvolutionSection.css';

// 进化 tab — first screen answers "这个员工最近有没有变强、有什么等我批".
//   1. 成长报告 (J2): zero-LLM production metrics the owner opens to judge growth.
//   2. 待你审批: held soul candidates with real approve/reject actions (the doorbell).
//   3. 技能试用: provisional skills under real-usage monitoring (J1) + lifecycle.

type AgentEvolutionSectionProps = {
  agentId: string;
  active: boolean;
  canManage?: boolean;
};

const STATE_COLORS: Record<string, { fg: string; bg: string }> = {
  active: { fg: 'var(--success)', bg: 'var(--success-subtle)' },
  stale: { fg: 'var(--warning)', bg: 'var(--warning-subtle)' },
  archived: { fg: 'var(--text-tertiary)', bg: 'var(--bg-secondary)' },
  provisional: { fg: 'var(--info)', bg: 'color-mix(in srgb, var(--info) 12%, transparent)' },
  rolled_back: { fg: 'var(--text-tertiary)', bg: 'var(--bg-secondary)' },
  needs_review: { fg: 'var(--warning)', bg: 'var(--warning-subtle)' },
};

function manifestTitle(item: EvolutionManifest): string {
  return String(item.skill_name || item.candidate_id || item.job_id || item.manifest_path || 'candidate');
}

function manifestStatus(item: EvolutionManifest): string {
  return String(item.status || item.reason || 'pending');
}

function GrowthReportCard({ metrics }: { metrics: GrowthMetrics | undefined }) {
  const { t } = useTranslation();
  if (!metrics) {
    return (
      <div className="agent-evolution-card">
        <h4 className="agent-evolution-heading">
          <IconChartLine size={16} /> {t('agent.evolution.growthHeading')}
        </h4>
        <p className="agent-evolution-muted-p">
          {t('agent.evolution.growthEmpty')}
        </p>
      </div>
    );
  }
  const failureModes = metrics.failure_modes ?? [];
  const reuse = metrics.reuse ?? {};
  const rework = metrics.rework ?? {};
  const polarity = metrics.feedback_polarity ?? {};
  const volume = metrics.task_volume ?? {};
  const evolution = metrics.evolution ?? {};
  return (
    <div className="agent-evolution-card">
      <div className="agent-evolution-report-head">
        <h4 className="agent-evolution-heading-flush">
          <IconChartLine size={16} /> {t('agent.evolution.growthHeading')}
        </h4>
        <span className="u-meta u-tertiary">
          {(metrics.generated_at ?? '').slice(0, 16)}
        </span>
      </div>

      {failureModes.length > 0 ? (
        <table className="agent-evolution-table">
          <thead>
            <tr className="agent-evolution-thead-row">
              <th className="agent-evolution-th">{t('agent.evolution.failureMode')}</th>
              <th className="agent-evolution-th">{t('agent.evolution.fmStatus')}</th>
              <th className="agent-evolution-th">{t('agent.evolution.recurred')}</th>
              <th className="agent-evolution-th">{t('agent.evolution.avoided')}</th>
              <th className="agent-evolution-th">{t('agent.evolution.avoidanceRate')}</th>
            </tr>
          </thead>
          <tbody>
            {failureModes.map((mode) => (
              <tr key={mode.id} className="agent-evolution-tr">
                <td className="agent-evolution-td">{mode.title}</td>
                <td className="agent-evolution-td">
                  <span className="badge">{mode.status || 'active'}</span>
                </td>
                <td className="agent-evolution-td" style={{ color: mode.recurred > 0 ? 'var(--error)' : undefined }}>{mode.recurred}</td>
                <td className="agent-evolution-td" style={{ color: mode.avoided > 0 ? 'var(--success)' : undefined }}>{mode.avoided}</td>
                <td className="agent-evolution-td">{mode.avoidance_rate != null ? `${Math.round(mode.avoidance_rate * 100)}%` : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="u-row u-tertiary">
          {t('agent.evolution.noFailureSignals')}
        </p>
      )}

      <div className="agent-evolution-metrics-grid">
        <div>
          📚 {t('agent.evolution.reuse')}: {reuse.total_citations ?? 0} {t('agent.evolution.citations')} / {reuse.knowledge_pages ?? 0} {t('agent.evolution.pages')}
        </div>
        <div>
          🔁 {t('agent.evolution.reworkRate')}: {rework.recent_rate != null ? `${Math.round((rework.recent_rate as number) * 100)}%` : '—'}
          {rework.previous_rate != null && ` (${t('agent.evolution.previously')} ${Math.round((rework.previous_rate as number) * 100)}%)`}
        </div>
        <div>
          👍 {t('agent.evolution.feedback')}: {polarity.recent?.useful ?? 0} useful / {polarity.recent?.misleading ?? 0} misleading
        </div>
        <div>
          ⚡ {t('agent.evolution.taskVolume')}: {volume.recent_invocations ?? 0} / {volume.window_days ?? 7}d
        </div>
        <div>
          🧬 {t('agent.evolution.promotions')}: {evolution.promotions ?? 0} · {t('agent.evolution.rollbacks')}: {evolution.rollbacks ?? 0}
        </div>
      </div>
    </div>
  );
}

function OwnerApprovalCard({
  agentId,
  candidates,
  canManage,
}: {
  agentId: string;
  candidates: EvolutionManifest[];
  canManage: boolean;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [expanded, setExpanded] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<string>('');

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['agent-evolution', agentId] });
    queryClient.invalidateQueries({ queryKey: ['knowledge-overview', agentId] });
  };
  const approveMutation = useMutation({
    mutationFn: (candidateId: string) => evolutionApi.approveSoulCandidate(agentId, candidateId),
    onSuccess: (result) => {
      setFeedback(
        result.status === 'committed'
          ? t('agent.evolution.approveDone')
          : `${t('agent.evolution.approveRefused')}: ${result.reason ?? ''}`,
      );
      invalidate();
    },
  });
  const rejectMutation = useMutation({
    mutationFn: (candidateId: string) => evolutionApi.rejectSoulCandidate(agentId, candidateId, ''),
    onSuccess: () => {
      setFeedback(t('agent.evolution.rejectDone'));
      invalidate();
    },
  });

  const pending = candidates.filter(
    (item) => String(item.status || '').toLowerCase() === 'held' && Boolean(item.requires_owner_approval),
  );
  if (!pending.length) return null;

  return (
    <div className="agent-evolution-card agent-evolution-card-accent">
      <h4 className="agent-evolution-heading">
        <IconShieldCheck size={16} /> {t('agent.evolution.approvalHeading')}
      </h4>
      <p className="u-row u-tertiary">
        {t(
          'agent.evolution.approvalHint',
        )}
      </p>
      {feedback && <p className="u-row u-secondary">{feedback}</p>}
      <div className="agent-evolution-stack">
        {pending.map((item) => {
          const candidateId = String(item.candidate_id ?? '');
          const isOpen = expanded === candidateId;
          return (
            <div key={candidateId} className="agent-evolution-card agent-evolution-card-inset">
              <div className="agent-evolution-row-between">
                <div className="agent-evolution-min0">
                  <code className="u-meta">{candidateId}</code>
                  <div className="agent-evolution-sub">
                    {String(item.reason ?? '')}
                  </div>
                </div>
                <div className="agent-evolution-actions">
                  <button className="btn btn-sm" onClick={() => setExpanded(isOpen ? null : candidateId)}>
                    {isOpen ? t('agent.evolution.collapse') : t('agent.evolution.viewPitch')}
                  </button>
                  {canManage && (
                    <>
                      <button
                        className="btn btn-sm btn-primary"
                        disabled={approveMutation.isPending}
                        onClick={() => approveMutation.mutate(candidateId)}
                      >
                        {t('agent.evolution.approve')}
                      </button>
                      <button
                        className="btn btn-sm"
                        disabled={rejectMutation.isPending}
                        onClick={() => rejectMutation.mutate(candidateId)}
                      >
                        {t('agent.evolution.reject')}
                      </button>
                    </>
                  )}
                </div>
              </div>
              {isOpen && (
                <pre className="agent-evolution-pitch">
                  {String(item.pitch ?? item.soul_pitch_md ?? item.reason ?? '')}
                </pre>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function AgentEvolutionSection({ agentId, active, canManage = false }: AgentEvolutionSectionProps) {
  const { t } = useTranslation();

  const { data, isLoading, isError } = useQuery({
    queryKey: ['agent-evolution', agentId],
    queryFn: () => evolutionApi.get(agentId),
    enabled: active && !!agentId,
  });
  const observabilityQuery = useQuery({
    queryKey: ['memory-observability', agentId],
    queryFn: () => knowledgeApi.observability(agentId),
    enabled: active && !!agentId,
  });

  const summary = data?.skill_ecosystem.summary;
  const skills = data?.skill_ecosystem.skills ?? [];
  const pendingT3Jobs = data?.memory_learning.pending_t3_jobs ?? [];
  const pendingSoulCandidates = data?.soul.pending_candidates ?? [];
  const skillCandidates = data?.skill_tuning.candidates ?? [];
  const provisionalSkills = skillCandidates.filter((item) => manifestStatus(item) === 'provisional');
  const otherSkillCandidates = skillCandidates.filter((item) => manifestStatus(item) !== 'provisional');
  const legacyFiles = data?.legacy_audit.detected_legacy_files ?? [];
  const timeline = data?.timeline ?? [];
  const growthMetrics = observabilityQuery.data?.growth?.metrics;

  const stateLabel = (state: string) => t(`agent.evolution.state.${state}`, state);
  const originLabel = (origin: string) => t(`agent.evolution.origin.${origin}`, origin);

  const summaryCards: {
    key: 'active' | 'stale' | 'archived' | 'evolvable';
    icon: ReactNode;
    colorKey: 'active' | 'stale' | 'archived';
  }[] = [
    { key: 'active', icon: <IconActivityHeartbeat size={16} />, colorKey: 'active' },
    { key: 'stale', icon: <IconSnowflake size={16} />, colorKey: 'stale' },
    { key: 'archived', icon: <IconArchive size={16} />, colorKey: 'archived' },
    { key: 'evolvable', icon: <IconSparkles size={16} />, colorKey: 'active' },
  ];

  return (
    <div className="agent-evolution-root">
      <div>
        <h3 className="agent-evolution-title">
          <IconBulb size={18} /> {t('agent.evolution.title')}
        </h3>
        <p className="agent-evolution-muted-p">
          {t('agent.evolution.description')}
        </p>
      </div>

      {isLoading && (
        <div className="u-body u-tertiary">{t('common.loading', 'Loading...')}</div>
      )}

      {isError && (
        <div className="u-body u-tertiary">{t('agent.evolution.empty')}</div>
      )}

      {!isLoading && !isError && (
        <>
          <GrowthReportCard metrics={growthMetrics} />

          <OwnerApprovalCard agentId={agentId} candidates={pendingSoulCandidates} canManage={canManage} />

          {provisionalSkills.length > 0 && (
            <div className="agent-evolution-card agent-evolution-card-accent">
              <h4 className="agent-evolution-heading-plain">🧪 {t('agent.evolution.provisionalHeading')}</h4>
              <p className="u-row u-tertiary">
                {t(
                  'agent.evolution.provisionalHint',
                )}
              </p>
              <div className="agent-evolution-list">
                {provisionalSkills.map((item, idx) => {
                  const runtimeSkill = skills.find(
                    (skill) => skill.last_candidate_id === item.candidate_id
                      || skill.skill_name === item.skill_name,
                  );
                  const trial = runtimeSkill?.trial;
                  return (
                    <div key={`${manifestTitle(item)}-${idx}`} className="agent-evolution-list-row">
                      <span>{manifestTitle(item)}</span>
                      <span className="agent-evolution-item-right">
                        {trial && (
                          <span className="u-meta u-tertiary">
                            {t('agent.evolution.trialPositive')} {trial.positive_count} / {trial.positive_threshold}
                            {' · '}
                            {t('agent.evolution.trialNegative')} {trial.negative_count} / {trial.negative_threshold}
                          </span>
                        )}
                        <span className="agent-evolution-state-badge agent-evolution-state-info">
                          provisional
                        </span>
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {summary && summary.total > 0 && (
            <div className="agent-evolution-summary-grid">
              {summaryCards.map(({ key, icon, colorKey }) => {
                const colors = STATE_COLORS[colorKey];
                return (
                  <div className="card agent-evolution-summary-card" key={key}>
                    <div className="agent-evolution-summary-label">
                      <span className="agent-evolution-icon" style={{ color: colors.fg }}>{icon}</span>
                      {t(`agent.evolution.summary.${key}`)}
                    </div>
                    <div className="agent-evolution-metric-value">{summary?.[key] ?? 0}</div>
                  </div>
                );
              })}
            </div>
          )}

          <TimelineList title={t('agent.evolution.timelineHeading', 'Timeline')} items={timeline} />

          {skills.length > 0 && (
            <div>
              <h4 className="agent-evolution-subheading">
                {t('agent.evolution.skillsHeading')}
              </h4>
              <div className="agent-evolution-list">
                {skills.map((skill) => {
                  const colors = STATE_COLORS[skill.state] ?? STATE_COLORS.archived;
                  return (
                    <div
                      key={`${skill.skill_name}-${skill.target_path ?? ''}`}
                      className="agent-evolution-item"
                    >
                      <div className="agent-evolution-min0">
                        <div className="agent-evolution-item-title">
                          {skill.skill_name}
                        </div>
                        <div className="agent-evolution-item-meta">
                          {originLabel(skill.skill_origin)}
                          {skill.target_path ? ` · ${skill.target_path}` : ''}
                        </div>
                      </div>
                      <div className="agent-evolution-item-right">
                        <span className="u-meta u-tertiary">
                          {t('agent.evolution.useCount', { count: skill.use_count })}
                        </span>
                        {skill.evolvable && (
                          <span className="agent-evolution-evolvable">
                            {t('agent.evolution.evolvable')}
                          </span>
                        )}
                        <span
                          className="agent-evolution-state-badge"
                          style={{ color: colors.fg, background: colors.bg }}
                        >
                          {stateLabel(skill.state)}
                        </span>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          <CandidateList
            title={t('agent.evolution.memoryJobsHeading')}
            icon={<IconHistory size={16} />}
            items={pendingT3Jobs}
          />
          <CandidateList
            title={t('agent.evolution.skillCandidatesHeading')}
            icon={<IconHistory size={16} />}
            items={otherSkillCandidates}
          />

          {legacyFiles.length > 0 && (
            <div>
              <h4 className="agent-evolution-subheading">
                {t('agent.evolution.legacyHeading')}
              </h4>
              <div className="agent-evolution-chips">
                {legacyFiles.map((file) => (
                  <code key={file} className="agent-evolution-legacy-chip">
                    {file}
                  </code>
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function TimelineList({ title, items }: { title: string; items: EvolutionTimelineItem[] }) {
  if (!items.length) return null;
  return (
    <div>
      <h4 className="agent-evolution-subheading">{title}</h4>
      <div className="agent-evolution-list">
        {items.slice(0, 24).map((item) => (
          <div key={item.id} className="agent-evolution-item">
            <div className="agent-evolution-min0">
              <div className="agent-evolution-item-title">
                {item.title}
              </div>
              <div className="agent-evolution-item-meta">
                {item.lane} · {item.stage}
                {item.path ? ` · ${item.path}` : ''}
              </div>
            </div>
            <span className="u-meta u-tertiary">{item.status}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function CandidateList({ title, icon, items }: { title: string; icon: ReactNode; items: EvolutionManifest[] }) {
  if (!items.length) return null;
  return (
    <div>
      <h4 className="agent-evolution-subheading-flex">
        {icon} {title}
      </h4>
      <div className="agent-evolution-list">
        {items.map((item, idx) => (
          <div key={`${manifestTitle(item)}-${idx}`} className="agent-evolution-item">
            <div className="agent-evolution-min0">
              <div className="agent-evolution-item-title">
                {manifestTitle(item)}
              </div>
              {item.manifest_path && (
                <div className="agent-evolution-item-meta">
                  {item.manifest_path}
                </div>
              )}
            </div>
            <span className="u-meta u-tertiary">{manifestStatus(item)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
