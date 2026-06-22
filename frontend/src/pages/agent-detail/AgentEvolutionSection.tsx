import { useQuery } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import {
  IconActivityHeartbeat,
  IconArchive,
  IconBulb,
  IconDatabase,
  IconHistory,
  IconSnowflake,
  IconSparkles,
} from '@tabler/icons-react';

import { evolutionApi, type EvolutionManifest, type EvolutionTimelineItem } from '../../api/domains/evolution';

type AgentEvolutionSectionProps = {
  agentId: string;
  active: boolean;
};

const STATE_COLORS: Record<string, { fg: string; bg: string }> = {
  active: { fg: 'var(--success, #16a34a)', bg: 'rgba(22,163,74,0.12)' },
  stale: { fg: 'var(--warning, #d97706)', bg: 'rgba(217,119,6,0.12)' },
  archived: { fg: 'var(--text-tertiary)', bg: 'var(--bg-secondary)' },
};

function manifestTitle(item: EvolutionManifest): string {
  return String(item.skill_name || item.candidate_id || item.job_id || item.manifest_path || 'candidate');
}

function manifestStatus(item: EvolutionManifest): string {
  return String(item.status || item.reason || 'pending');
}

export default function AgentEvolutionSection({ agentId, active }: AgentEvolutionSectionProps) {
  const { t } = useTranslation();

  const { data, isLoading, isError } = useQuery({
    queryKey: ['agent-evolution', agentId],
    queryFn: () => evolutionApi.get(agentId),
    enabled: active && !!agentId,
  });

  const summary = data?.skill_ecosystem.summary;
  const skills = data?.skill_ecosystem.skills ?? [];
  const pendingT3Jobs = data?.memory_learning.pending_t3_jobs ?? [];
  const pendingSoulCandidates = data?.soul.pending_candidates ?? [];
  const skillCandidates = data?.skill_tuning.candidates ?? [];
  const legacyFiles = data?.legacy_audit.detected_legacy_files ?? [];
  const timeline = data?.timeline ?? [];
  const hasAny =
    !!summary &&
    (summary.total > 0 ||
      timeline.length > 0 ||
      pendingT3Jobs.length > 0 ||
      pendingSoulCandidates.length > 0 ||
      skillCandidates.length > 0 ||
      legacyFiles.length > 0);

  const stateLabel = (state: string) => t(`agent.evolution.state.${state}`, state);
  const originLabel = (origin: string) => t(`agent.evolution.origin.${origin}`, origin);
  const pathRows: { key: string; path?: string }[] = [
    { key: 'memory', path: data?.path_contract.t3_capabilities },
    { key: 'soul', path: data?.path_contract.soul },
    { key: 'skillRegistry', path: data?.path_contract.skill_registry },
    { key: 'skillCandidates', path: data?.path_contract.skill_candidates },
  ];

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

      {!isLoading && !isError && !hasAny && (
        <div
          style={{
            padding: '32px 16px',
            textAlign: 'center',
            fontSize: '13px',
            color: 'var(--text-tertiary)',
            border: '1px dashed var(--border-subtle)',
            borderRadius: '8px',
          }}
        >
          {t('agent.evolution.empty')}
        </div>
      )}

      {!isLoading && !isError && hasAny && (
        <>
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

          <div>
            <h4 style={{ fontSize: '13px', marginBottom: '8px', color: 'var(--text-secondary)' }}>
              {t('agent.evolution.pathsHeading')}
            </h4>
            <div style={{ display: 'grid', gap: '6px' }}>
              {pathRows.map(({ key, path }) => (
                <div
                  key={key}
                  style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    gap: '12px',
                    padding: '9px 12px',
                    borderRadius: '8px',
                    background: 'var(--bg-secondary)',
                    fontSize: '12px',
                  }}
                >
                  <span style={{ color: 'var(--text-tertiary)' }}>{t(`agent.evolution.path.${key}`)}</span>
                  <code style={{ color: 'var(--text-secondary)', overflowWrap: 'anywhere' }}>{path}</code>
                </div>
              ))}
            </div>
          </div>

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
            icon={<IconDatabase size={16} />}
            items={pendingT3Jobs}
          />
          <CandidateList
            title={t('agent.evolution.soulCandidatesHeading')}
            icon={<IconSparkles size={16} />}
            items={pendingSoulCandidates}
          />
          <CandidateList
            title={t('agent.evolution.skillCandidatesHeading')}
            icon={<IconHistory size={16} />}
            items={skillCandidates}
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
