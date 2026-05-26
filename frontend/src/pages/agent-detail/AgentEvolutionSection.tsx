import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  IconActivityHeartbeat,
  IconArchive,
  IconBulb,
  IconClock,
  IconHistory,
  IconSnowflake,
} from '@tabler/icons-react';

import { evolutionApi, type EvolutionTimelineItem } from '../../api/domains/evolution';

type AgentEvolutionSectionProps = {
  agentId: string;
  active: boolean;
};

const STATE_COLORS: Record<string, { fg: string; bg: string }> = {
  active: { fg: 'var(--success, #16a34a)', bg: 'rgba(22,163,74,0.12)' },
  stale: { fg: 'var(--warning, #d97706)', bg: 'rgba(217,119,6,0.12)' },
  archived: { fg: 'var(--text-tertiary)', bg: 'var(--bg-secondary)' },
};

const TIMELINE_KIND_COLORS: Record<string, string> = {
  promote: 'var(--success, #16a34a)',
  promotion: 'var(--success, #16a34a)',
  created: 'var(--accent-primary)',
  candidate: 'var(--accent-primary)',
  eval: 'var(--text-secondary)',
  patch: 'var(--accent-primary)',
  stale: 'var(--warning, #d97706)',
  archived: 'var(--text-tertiary)',
  rollback: 'var(--danger, #dc2626)',
};

function formatTimestamp(iso: string): string {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toLocaleString();
}

export default function AgentEvolutionSection({ agentId, active }: AgentEvolutionSectionProps) {
  const { t } = useTranslation();

  const { data, isLoading, isError } = useQuery({
    queryKey: ['agent-evolution', agentId],
    queryFn: () => evolutionApi.get(agentId),
    enabled: active && !!agentId,
  });

  const summary = data?.skill_summary;
  const skills = data?.skills ?? [];
  const timeline = data?.timeline ?? [];
  const hasAny = !!summary && summary.total > 0;
  const hasTimeline = timeline.length > 0;

  const stateLabel = (state: string) => t(`agent.evolution.state.${state}`, state);
  const kindLabel = (kind: string) => t(`agent.evolution.kind.${kind}`, kind);

  const summaryCards: { key: 'active' | 'stale' | 'archived'; icon: React.ReactNode }[] = [
    { key: 'active', icon: <IconActivityHeartbeat size={16} /> },
    { key: 'stale', icon: <IconSnowflake size={16} /> },
    { key: 'archived', icon: <IconArchive size={16} /> },
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
        <div style={{ fontSize: '13px', color: 'var(--text-tertiary)' }}>{t('common.loading', 'Loading…')}</div>
      )}

      {isError && (
        <div style={{ fontSize: '13px', color: 'var(--text-tertiary)' }}>{t('agent.evolution.empty')}</div>
      )}

      {!isLoading && !isError && !hasAny && !hasTimeline && (
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
          {/* ── Skill state summary ── */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: '12px' }}>
            {summaryCards.map(({ key, icon }) => {
              const colors = STATE_COLORS[key];
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
                    {stateLabel(key)}
                  </div>
                  <div style={{ fontSize: '22px', fontWeight: 600 }}>{summary?.[key] ?? 0}</div>
                </div>
              );
            })}
          </div>

          {/* ── Skill list ── */}
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
                      key={skill.slug}
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'space-between',
                        gap: '12px',
                        padding: '10px 12px',
                        borderRadius: '8px',
                        background: 'var(--bg-secondary)',
                      }}
                    >
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', minWidth: 0 }}>
                        <span
                          style={{
                            fontSize: '13px',
                            fontWeight: 500,
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap',
                          }}
                        >
                          {skill.slug}
                        </span>
                        {skill.pinned && (
                          <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                            {t('agent.evolution.pinned')}
                          </span>
                        )}
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexShrink: 0 }}>
                        <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                          {t('agent.evolution.useCount', { count: skill.use_count })}
                        </span>
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
        </>
      )}

      {/* ── Evolution timeline ── */}
      {!isLoading && !isError && hasTimeline && (
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
            <IconHistory size={16} /> {t('agent.evolution.timelineHeading')}
          </h4>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
            {timeline.map((item: EvolutionTimelineItem, idx: number) => (
              <div
                key={`${item.at}-${idx}`}
                style={{
                  display: 'flex',
                  gap: '10px',
                  paddingLeft: '12px',
                  borderLeft: `2px solid ${TIMELINE_KIND_COLORS[item.kind] ?? 'var(--border-subtle)'}`,
                }}
              >
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
                    <span
                      style={{
                        fontSize: '11px',
                        fontWeight: 600,
                        textTransform: 'uppercase',
                        letterSpacing: '0.03em',
                        color: TIMELINE_KIND_COLORS[item.kind] ?? 'var(--text-secondary)',
                      }}
                    >
                      {kindLabel(item.kind)}
                    </span>
                    <span
                      style={{
                        fontSize: '11px',
                        color: 'var(--text-tertiary)',
                        display: 'inline-flex',
                        alignItems: 'center',
                        gap: '3px',
                      }}
                    >
                      <IconClock size={12} /> {formatTimestamp(item.at)}
                    </span>
                  </div>
                  <div style={{ fontSize: '13px', marginTop: '2px' }}>{item.title}</div>
                  {item.detail && (
                    <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                      {item.detail}
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
