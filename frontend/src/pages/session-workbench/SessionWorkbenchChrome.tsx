import React from 'react';
import { useTranslation } from 'react-i18next';
import {
  IconActivity,
  IconShieldCheck,
  IconChecklist,
  IconDatabase,
  IconGitBranch,
  IconGitCommit,
  IconInfoCircle,
  IconPlayerPlay,
} from '@tabler/icons-react';

import type { SessionWorkbenchHeaderModel, SessionWorkbenchInspectorModel } from './timelineModel';
import './SessionWorkbenchChrome.css';

function shortId(id: string | null): string {
  if (!id) return '';
  return id.length <= 8 ? id : id.slice(0, 8);
}

function statusColor(status: SessionWorkbenchHeaderModel['status']): string {
  if (status === 'running' || status === 'streaming') return 'var(--accent-primary)';
  if (status === 'waiting') return 'var(--warning)';
  if (status === 'failed') return 'var(--error)';
  return 'var(--text-tertiary)';
}

function HeaderChip({
  label,
  value,
  icon,
  title,
}: {
  label: string;
  value: React.ReactNode;
  icon?: React.ReactNode;
  title?: string;
}) {
  return (
    <span className="swb-chip" title={title || label}>
      {icon}
      <span className="swb-chip-label">{label}</span>
      <strong className="swb-chip-value">{value}</strong>
    </span>
  );
}

export function SessionWorkbenchHeader({ model }: { model: SessionWorkbenchHeaderModel }) {
  const { t } = useTranslation();
  const statusLabel = t(`sessionWorkbench.status.${model.status}`, model.status);
  return (
    <div data-testid="session-workbench-header" className="swb-header">
      <div className="swb-header-title-row">
        <h2 className="swb-header-title">{model.title}</h2>
        {model.sessionId && <span className="swb-header-id">{shortId(model.sessionId)}</span>}
        <span className="swb-header-status">
          <span className="swb-status-dot" style={{ background: statusColor(model.status) }} />
          <span>{statusLabel}</span>
          {model.modelLabel && <span>· {model.modelLabel}</span>}
          {model.providerLabel && <span>· {model.providerLabel}</span>}
        </span>
      </div>
      <div className="swb-header-chips">
        <HeaderChip label={t('sessionWorkbench.resumeHealth', 'resume')} value={model.resumeHealth} icon={<IconActivity size={12} />} />
        {model.permissionMode && (
          <HeaderChip
            label={t('sessionWorkbench.permission', 'permission')}
            value={model.permissionMode}
            title={model.governanceLabel || undefined}
            icon={<IconShieldCheck size={12} />}
          />
        )}
        {model.governanceLabel && (
          <HeaderChip
            label={t('sessionWorkbench.governance', 'governance')}
            value={model.governanceLabel}
            icon={<IconShieldCheck size={12} />}
          />
        )}
        {model.activeProjection && (
          <HeaderChip
            label={t('sessionWorkbench.activeProjection', 'projection')}
            value={model.activeProjection}
            icon={<IconDatabase size={12} />}
          />
        )}
        <HeaderChip label={t('sessionWorkbench.checkpoints', 'checkpoints')} value={model.checkpointCount} icon={<IconGitCommit size={12} />} />
        <HeaderChip label={t('sessionWorkbench.branchDepth', 'branch')} value={model.branchDepth} icon={<IconGitBranch size={12} />} />
        <HeaderChip label={t('sessionWorkbench.compactions', 'compactions')} value={model.compactionCount} icon={<IconDatabase size={12} />} />
        {model.contextWindowStatusLabel && (
          <HeaderChip
            label={t('sessionWorkbench.contextWindow', 'context')}
            value={model.contextWindowStatusLabel}
            title={model.contextWindowTitle || undefined}
            icon={<IconDatabase size={12} />}
          />
        )}
        {model.activeRunStatus && (
          <HeaderChip label={t('sessionWorkbench.activeRun', 'run')} value={model.activeRunStatus} icon={<IconPlayerPlay size={12} />} />
        )}
      </div>
    </div>
  );
}

function InspectorRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="swb-inspector-row">
      <span className="swb-inspector-row-label">{label}</span>
      <span className="swb-inspector-row-value">{value}</span>
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
    <aside data-testid="session-workbench-inspector" className="swb-inspector">
      <div className="swb-inspector-head">
        <div className="swb-inspector-title">
          <IconInfoCircle size={15} />
          <strong>{t('sessionWorkbench.inspectorTitle', 'Session context')}</strong>
        </div>
        <div className="swb-inspector-rows">
          <InspectorRow label={t('sessionWorkbench.events', 'events')} value={model.sessionEventCount ?? '-'} />
          <InspectorRow label={t('sessionWorkbench.t0Segments', 'T0 segments')} value={model.t0SegmentCount} />
          <InspectorRow label={t('sessionWorkbench.latestCheckpoint', 'latest checkpoint')} value={model.latestCheckpointLabel || '-'} />
          <InspectorRow label={t('sessionWorkbench.usedTools', 'used tools')} value={model.usedToolCount} />
          <InspectorRow label={t('sessionWorkbench.blockedCapabilities', 'blocked')} value={model.blockedCapabilityCount} />
          <InspectorRow label={t('sessionWorkbench.toolGroups', 'tool groups')} value={model.activatedToolGroupCount} />
        </div>
      </div>
      <div className="swb-inspector-body">
        {children}
        <div className="swb-inspector-hint">
          <div className="swb-inspector-hint-title">
            <IconChecklist size={13} />
            {t('sessionWorkbench.nativeControls', 'Session-native controls')}
          </div>
          {t('sessionWorkbench.nativeControlsHint', 'Goal, checkpoint, branch, team, sources, and raw tool detail belong here or in inline disclosures, not as permanent command bars.')}
        </div>
      </div>
    </aside>
  );
}
