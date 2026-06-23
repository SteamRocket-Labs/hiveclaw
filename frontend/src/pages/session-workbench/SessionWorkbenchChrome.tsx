import React from 'react';
import { useTranslation } from 'react-i18next';
import {
  IconActivity,
  IconChecklist,
  IconDatabase,
  IconGitBranch,
  IconGitCommit,
  IconInfoCircle,
  IconPlayerPlay,
} from '@tabler/icons-react';

import type { SessionWorkbenchHeaderModel, SessionWorkbenchInspectorModel } from './timelineModel';

function shortId(id: string | null): string {
  if (!id) return '';
  return id.length <= 8 ? id : id.slice(0, 8);
}

function statusColor(status: SessionWorkbenchHeaderModel['status']): string {
  if (status === 'running' || status === 'streaming') return 'var(--accent-primary)';
  if (status === 'waiting') return 'rgb(217,119,6)';
  if (status === 'failed') return 'rgb(220,38,38)';
  return 'var(--text-tertiary)';
}

function HeaderChip({
  label,
  value,
  icon,
}: {
  label: string;
  value: React.ReactNode;
  icon?: React.ReactNode;
}) {
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: '5px',
        minHeight: '24px',
        padding: '3px 7px',
        borderRadius: '6px',
        border: '1px solid var(--border-subtle)',
        background: 'var(--bg-secondary)',
        color: 'var(--text-secondary)',
        fontSize: '11px',
        whiteSpace: 'nowrap',
      }}
      title={label}
    >
      {icon}
      <span style={{ color: 'var(--text-tertiary)' }}>{label}</span>
      <strong style={{ color: 'var(--text-primary)', fontWeight: 600 }}>{value}</strong>
    </span>
  );
}

export function SessionWorkbenchHeader({ model }: { model: SessionWorkbenchHeaderModel }) {
  const { t } = useTranslation();
  const statusLabel = t(`sessionWorkbench.status.${model.status}`, model.status);
  return (
    <div
      data-testid="session-workbench-header"
      style={{
        borderBottom: '1px solid var(--border-subtle)',
        background: 'var(--bg-elevated)',
        padding: '10px 14px',
        display: 'grid',
        gap: '8px',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: '10px', minWidth: 0 }}>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', minWidth: 0 }}>
            <h2
              style={{
                margin: 0,
                fontSize: '15px',
                fontWeight: 700,
                color: 'var(--text-primary)',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {model.title}
            </h2>
            {model.sessionId && (
              <span style={{ fontSize: '10px', color: 'var(--text-tertiary)', fontVariantNumeric: 'tabular-nums' }}>
                {shortId(model.sessionId)}
              </span>
            )}
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginTop: '4px', fontSize: '11px', color: 'var(--text-tertiary)' }}>
            <span
              style={{
                display: 'inline-block',
                width: '6px',
                height: '6px',
                borderRadius: '50%',
                background: statusColor(model.status),
              }}
            />
            <span>{statusLabel}</span>
            {model.modelLabel && <span>· {model.modelLabel}</span>}
            {model.providerLabel && <span>· {model.providerLabel}</span>}
          </div>
        </div>
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
        <HeaderChip label={t('sessionWorkbench.resumeHealth', 'resume')} value={model.resumeHealth} icon={<IconActivity size={13} />} />
        <HeaderChip label={t('sessionWorkbench.checkpoints', 'checkpoints')} value={model.checkpointCount} icon={<IconGitCommit size={13} />} />
        <HeaderChip label={t('sessionWorkbench.branchDepth', 'branch')} value={model.branchDepth} icon={<IconGitBranch size={13} />} />
        <HeaderChip label={t('sessionWorkbench.compactions', 'compactions')} value={model.compactionCount} icon={<IconDatabase size={13} />} />
        {model.activeRunStatus && (
          <HeaderChip label={t('sessionWorkbench.activeRun', 'run')} value={model.activeRunStatus} icon={<IconPlayerPlay size={13} />} />
        )}
      </div>
    </div>
  );
}

function InspectorRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '10px', fontSize: '11px' }}>
      <span style={{ color: 'var(--text-tertiary)' }}>{label}</span>
      <span style={{ color: 'var(--text-primary)', fontWeight: 600, textAlign: 'right' }}>{value}</span>
    </div>
  );
}

export function SessionWorkbenchInspector({
  model,
  children,
}: {
  model: SessionWorkbenchInspectorModel;
  children?: React.ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <aside
      data-testid="session-workbench-inspector"
      style={{
        width: '292px',
        flexShrink: 0,
        borderLeft: '1px solid var(--border-subtle)',
        background: 'var(--bg-elevated)',
        display: 'flex',
        flexDirection: 'column',
        minHeight: 0,
        overflow: 'hidden',
      }}
    >
      <div style={{ padding: '12px 14px', borderBottom: '1px solid var(--border-subtle)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '7px', marginBottom: '10px' }}>
          <IconInfoCircle size={15} color="var(--text-tertiary)" />
          <strong style={{ fontSize: '12px', color: 'var(--text-primary)' }}>
            {t('sessionWorkbench.inspectorTitle', 'Session context')}
          </strong>
        </div>
        <div style={{ display: 'grid', gap: '7px' }}>
          <InspectorRow label={t('sessionWorkbench.events', 'events')} value={model.sessionEventCount ?? '-'} />
          <InspectorRow label={t('sessionWorkbench.t0Segments', 'T0 segments')} value={model.t0SegmentCount} />
          <InspectorRow label={t('sessionWorkbench.latestCheckpoint', 'latest checkpoint')} value={model.latestCheckpointLabel || '-'} />
          <InspectorRow label={t('sessionWorkbench.usedTools', 'used tools')} value={model.usedToolCount} />
          <InspectorRow label={t('sessionWorkbench.blockedCapabilities', 'blocked')} value={model.blockedCapabilityCount} />
          <InspectorRow label={t('sessionWorkbench.toolGroups', 'tool groups')} value={model.activatedToolGroupCount} />
        </div>
      </div>
      <div style={{ flex: 1, overflow: 'auto', padding: '10px 12px', display: 'grid', gap: '10px', alignContent: 'start' }}>
        {children}
        <div
          style={{
            border: '1px solid var(--border-subtle)',
            borderRadius: '8px',
            padding: '10px',
            background: 'var(--bg-secondary)',
            fontSize: '11px',
            color: 'var(--text-tertiary)',
            lineHeight: 1.5,
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px', color: 'var(--text-secondary)', fontWeight: 600, marginBottom: '4px' }}>
            <IconChecklist size={13} />
            {t('sessionWorkbench.nativeControls', 'Session-native controls')}
          </div>
          {t('sessionWorkbench.nativeControlsHint', 'Goal, checkpoint, branch, team, sources, and raw tool detail belong here or in inline disclosures, not as permanent command bars.')}
        </div>
      </div>
    </aside>
  );
}
