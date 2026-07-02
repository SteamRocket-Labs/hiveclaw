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
  active: { fg: 'var(--success, #16a34a)', bg: 'rgba(22,163,74,0.12)' },
  stale: { fg: 'var(--warning, #d97706)', bg: 'rgba(217,119,6,0.12)' },
  archived: { fg: 'var(--text-tertiary)', bg: 'var(--bg-secondary)' },
  provisional: { fg: '#2563eb', bg: 'rgba(37,99,235,0.12)' },
};

const cardStyle: React.CSSProperties = {
  border: '1px solid var(--border-primary)',
  borderRadius: '8px',
  padding: '12px 16px',
  background: 'var(--bg-secondary)',
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
      <div style={cardStyle}>
        <h4 style={{ marginBottom: '8px', display: 'flex', alignItems: 'center', gap: '6px' }}>
          <IconChartLine size={16} /> {t('agent.evolution.growthHeading', '成长报告')}
        </h4>
        <p style={{ fontSize: '13px', color: 'var(--text-tertiary)', margin: 0 }}>
          {t('agent.evolution.growthEmpty', '还没有成长报告 — 随心跳自动生成,数字来自真实工作证据。')}
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
    <div style={cardStyle}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: '10px' }}>
        <h4 style={{ display: 'flex', alignItems: 'center', gap: '6px', margin: 0 }}>
          <IconChartLine size={16} /> {t('agent.evolution.growthHeading', '成长报告')}
        </h4>
        <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
          {(metrics.generated_at ?? '').slice(0, 16)}
        </span>
      </div>

      {failureModes.length > 0 ? (
        <table style={{ width: '100%', fontSize: '12px', borderCollapse: 'collapse', marginBottom: '10px' }}>
          <thead>
            <tr style={{ textAlign: 'left', color: 'var(--text-tertiary)' }}>
              <th style={{ padding: '4px' }}>{t('agent.evolution.failureMode', '失败模式')}</th>
              <th style={{ padding: '4px' }}>{t('agent.evolution.fmStatus', '状态')}</th>
              <th style={{ padding: '4px' }}>{t('agent.evolution.recurred', '复发')}</th>
              <th style={{ padding: '4px' }}>{t('agent.evolution.avoided', '规避')}</th>
              <th style={{ padding: '4px' }}>{t('agent.evolution.avoidanceRate', '规避率')}</th>
            </tr>
          </thead>
          <tbody>
            {failureModes.map((mode) => (
              <tr key={mode.id} style={{ borderTop: '1px solid var(--border-primary)' }}>
                <td style={{ padding: '4px' }}>{mode.title}</td>
                <td style={{ padding: '4px' }}>
                  <span className="badge">{mode.status || 'active'}</span>
                </td>
                <td style={{ padding: '4px', color: mode.recurred > 0 ? '#ef4444' : undefined }}>{mode.recurred}</td>
                <td style={{ padding: '4px', color: mode.avoided > 0 ? '#22c55e' : undefined }}>{mode.avoided}</td>
                <td style={{ padding: '4px' }}>{mode.avoidance_rate != null ? `${Math.round(mode.avoidance_rate * 100)}%` : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
          {t('agent.evolution.noFailureSignals', '暂无失败模式信号。')}
        </p>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: '8px', fontSize: '12px', color: 'var(--text-secondary)' }}>
        <div>
          📚 {t('agent.evolution.reuse', '知识复用')}: {reuse.total_citations ?? 0} {t('agent.evolution.citations', '次引用')} / {reuse.knowledge_pages ?? 0} {t('agent.evolution.pages', '页')}
        </div>
        <div>
          🔁 {t('agent.evolution.reworkRate', '返工率')}: {rework.recent_rate != null ? `${Math.round((rework.recent_rate as number) * 100)}%` : '—'}
          {rework.previous_rate != null && ` (${t('agent.evolution.previously', '此前')} ${Math.round((rework.previous_rate as number) * 100)}%)`}
        </div>
        <div>
          👍 {t('agent.evolution.feedback', '主人反馈')}: {polarity.recent?.useful ?? 0} useful / {polarity.recent?.misleading ?? 0} misleading
        </div>
        <div>
          ⚡ {t('agent.evolution.taskVolume', '任务量')}: {volume.recent_invocations ?? 0} / {volume.window_days ?? 7}d
        </div>
        <div>
          🧬 {t('agent.evolution.promotions', '晋升')}: {evolution.promotions ?? 0} · {t('agent.evolution.rollbacks', '回滚')}: {evolution.rollbacks ?? 0}
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
          ? t('agent.evolution.approveDone', '已批准并写入 soul（可回滚）。')
          : `${t('agent.evolution.approveRefused', '门禁拒绝')}: ${result.reason ?? ''}`,
      );
      invalidate();
    },
  });
  const rejectMutation = useMutation({
    mutationFn: (candidateId: string) => evolutionApi.rejectSoulCandidate(agentId, candidateId, ''),
    onSuccess: () => {
      setFeedback(t('agent.evolution.rejectDone', '已驳回。'));
      invalidate();
    },
  });

  const pending = candidates.filter(
    (item) => String(item.status || '').toLowerCase() === 'held' && Boolean(item.requires_owner_approval),
  );
  if (!pending.length) return null;

  return (
    <div style={{ ...cardStyle, borderColor: '#2563eb' }}>
      <h4 style={{ marginBottom: '8px', display: 'flex', alignItems: 'center', gap: '6px' }}>
        <IconShieldCheck size={16} /> {t('agent.evolution.approvalHeading', '待你审批 — soul 变更提名')}
      </h4>
      <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginTop: 0 }}>
        {t(
          'agent.evolution.approvalHint',
          '这些提名触碰敏感面,agent 不能自行写入。批准会经过硬门禁并保留回滚快照。',
        )}
      </p>
      {feedback && <p style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>{feedback}</p>}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
        {pending.map((item) => {
          const candidateId = String(item.candidate_id ?? '');
          const isOpen = expanded === candidateId;
          return (
            <div key={candidateId} style={{ ...cardStyle, background: 'var(--bg-primary)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: '12px', alignItems: 'center' }}>
                <div style={{ minWidth: 0 }}>
                  <code style={{ fontSize: '11px' }}>{candidateId}</code>
                  <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '2px' }}>
                    {String(item.reason ?? '')}
                  </div>
                </div>
                <div style={{ display: 'flex', gap: '6px', flexShrink: 0 }}>
                  <button className="btn btn-sm" onClick={() => setExpanded(isOpen ? null : candidateId)}>
                    {isOpen ? t('agent.evolution.collapse', '收起') : t('agent.evolution.viewPitch', '看提名理由')}
                  </button>
                  {canManage && (
                    <>
                      <button
                        className="btn btn-sm btn-primary"
                        disabled={approveMutation.isPending}
                        onClick={() => approveMutation.mutate(candidateId)}
                      >
                        {t('agent.evolution.approve', '批准')}
                      </button>
                      <button
                        className="btn btn-sm"
                        disabled={rejectMutation.isPending}
                        onClick={() => rejectMutation.mutate(candidateId)}
                      >
                        {t('agent.evolution.reject', '驳回')}
                      </button>
                    </>
                  )}
                </div>
              </div>
              {isOpen && (
                <pre
                  style={{
                    fontSize: '12px',
                    whiteSpace: 'pre-wrap',
                    marginTop: '8px',
                    color: 'var(--text-secondary)',
                  }}
                >
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
    <div style={{ display: 'flex', flexDirection: 'column', gap: '24px' }}>
      <div>
        <h3 style={{ marginBottom: '4px', display: 'flex', alignItems: 'center', gap: '8px' }}>
          <IconBulb size={18} /> {t('agent.evolution.title')}
        </h3>
        <p style={{ fontSize: '13px', color: 'var(--text-tertiary)', margin: 0 }}>
          {t('agent.evolution.description')}
        </p>
      </div>

      {isLoading && (
        <div style={{ fontSize: '13px', color: 'var(--text-tertiary)' }}>{t('common.loading', 'Loading...')}</div>
      )}

      {isError && (
        <div style={{ fontSize: '13px', color: 'var(--text-tertiary)' }}>{t('agent.evolution.empty')}</div>
      )}

      {!isLoading && !isError && (
        <>
          <GrowthReportCard metrics={growthMetrics} />

          <OwnerApprovalCard agentId={agentId} candidates={pendingSoulCandidates} canManage={canManage} />

          {provisionalSkills.length > 0 && (
            <div style={{ ...cardStyle, borderColor: STATE_COLORS.provisional.fg }}>
              <h4 style={{ marginBottom: '8px' }}>🧪 {t('agent.evolution.provisionalHeading', '技能试用中')}</h4>
              <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginTop: 0 }}>
                {t(
                  'agent.evolution.provisionalHint',
                  '已生效并受监控:真实使用积累后自动转正,负信号超阈自动回滚。',
                )}
              </p>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                {provisionalSkills.map((item, idx) => (
                  <div
                    key={`${manifestTitle(item)}-${idx}`}
                    style={{ display: 'flex', justifyContent: 'space-between', gap: '12px', fontSize: '13px' }}
                  >
                    <span>{manifestTitle(item)}</span>
                    <span
                      style={{
                        fontSize: '11px',
                        fontWeight: 600,
                        padding: '2px 8px',
                        borderRadius: '999px',
                        color: STATE_COLORS.provisional.fg,
                        background: STATE_COLORS.provisional.bg,
                      }}
                    >
                      provisional
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {summary && summary.total > 0 && (
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0, 1fr))', gap: '12px' }}>
              {summaryCards.map(({ key, icon, colorKey }) => {
                const colors = STATE_COLORS[colorKey];
                return (
                  <div className="card" key={key} style={{ padding: '12px 14px' }}>
                    <div
                      style={{
                        fontSize: '12px',
                        color: 'var(--text-tertiary)',
                        marginBottom: '6px',
                        display: 'flex',
                        alignItems: 'center',
                        gap: '6px',
                      }}
                    >
                      <span style={{ color: colors.fg, display: 'inline-flex' }}>{icon}</span>
                      {t(`agent.evolution.summary.${key}`)}
                    </div>
                    <div style={{ fontSize: '22px', fontWeight: 600 }}>{summary?.[key] ?? 0}</div>
                  </div>
                );
              })}
            </div>
          )}

          <TimelineList title={t('agent.evolution.timelineHeading', 'Timeline')} items={timeline} />

          {skills.length > 0 && (
            <div>
              <h4 style={{ fontSize: '13px', marginBottom: '8px', color: 'var(--text-secondary)' }}>
                {t('agent.evolution.skillsHeading')}
              </h4>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                {skills.map((skill) => {
                  const colors = STATE_COLORS[skill.state] ?? STATE_COLORS.archived;
                  return (
                    <div
                      key={`${skill.skill_name}-${skill.target_path ?? ''}`}
                      style={{
                        display: 'grid',
                        gridTemplateColumns: 'minmax(0, 1fr) auto',
                        gap: '12px',
                        padding: '10px 12px',
                        borderRadius: '8px',
                        background: 'var(--bg-secondary)',
                      }}
                    >
                      <div style={{ minWidth: 0 }}>
                        <div
                          style={{
                            fontSize: '13px',
                            fontWeight: 500,
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap',
                          }}
                        >
                          {skill.skill_name}
                        </div>
                        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                          {originLabel(skill.skill_origin)}
                          {skill.target_path ? ` · ${skill.target_path}` : ''}
                        </div>
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
                        <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                          {t('agent.evolution.useCount', { count: skill.use_count })}
                        </span>
                        {skill.evolvable && (
                          <span style={{ fontSize: '11px', color: 'var(--success, #16a34a)' }}>
                            {t('agent.evolution.evolvable')}
                          </span>
                        )}
                        <span
                          style={{
                            fontSize: '11px',
                            fontWeight: 600,
                            padding: '2px 8px',
                            borderRadius: '999px',
                            color: colors.fg,
                            background: colors.bg,
                          }}
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
              <h4 style={{ fontSize: '13px', marginBottom: '8px', color: 'var(--text-secondary)' }}>
                {t('agent.evolution.legacyHeading')}
              </h4>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
                {legacyFiles.map((file) => (
                  <code
                    key={file}
                    style={{
                      fontSize: '11px',
                      padding: '4px 7px',
                      borderRadius: '6px',
                      background: 'var(--bg-secondary)',
                      color: 'var(--text-tertiary)',
                    }}
                  >
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
      <h4 style={{ fontSize: '13px', marginBottom: '8px', color: 'var(--text-secondary)' }}>{title}</h4>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
        {items.slice(0, 24).map((item) => (
          <div
            key={item.id}
            style={{
              display: 'grid',
              gridTemplateColumns: 'minmax(0, 1fr) auto',
              gap: '12px',
              padding: '10px 12px',
              borderRadius: '8px',
              background: 'var(--bg-secondary)',
            }}
          >
            <div style={{ minWidth: 0 }}>
              <div style={{ fontSize: '13px', fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {item.title}
              </div>
              <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                {item.lane} · {item.stage}
                {item.path ? ` · ${item.path}` : ''}
              </div>
            </div>
            <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{item.status}</span>
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
      <h4
        style={{
          fontSize: '13px',
          marginBottom: '8px',
          color: 'var(--text-secondary)',
          display: 'flex',
          alignItems: 'center',
          gap: '6px',
        }}
      >
        {icon} {title}
      </h4>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
        {items.map((item, idx) => (
          <div
            key={`${manifestTitle(item)}-${idx}`}
            style={{
              display: 'grid',
              gridTemplateColumns: 'minmax(0, 1fr) auto',
              gap: '12px',
              padding: '10px 12px',
              borderRadius: '8px',
              background: 'var(--bg-secondary)',
            }}
          >
            <div style={{ minWidth: 0 }}>
              <div
                style={{
                  fontSize: '13px',
                  fontWeight: 500,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
              >
                {manifestTitle(item)}
              </div>
              {item.manifest_path && (
                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                  {item.manifest_path}
                </div>
              )}
            </div>
            <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{manifestStatus(item)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
