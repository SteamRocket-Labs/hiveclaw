import React from 'react';
import { useTranslation } from 'react-i18next';

import type { FeishuRuntimeStatus } from '../api/domains/tools';

type FeishuRuntimeStatusCardProps = {
  status: FeishuRuntimeStatus;
  compact?: boolean;
  isAdmin?: boolean;
};

function boolKey(value: boolean, positive: string, negative: string) {
  return value ? positive : negative;
}

function statusTone(status: FeishuRuntimeStatus) {
  if (status.base_tasks_ready) return { key: 'Ready', fallback: 'Ready', color: 'var(--success)' };
  if (status.ok) return { key: 'NeedsAttention', fallback: 'Needs Attention', color: 'var(--warning, #f59e0b)' };
  return { key: 'Disabled', fallback: 'Disabled', color: 'var(--error)' };
}

export function FeishuRuntimeStatusCard({ status, compact = false, isAdmin = false }: FeishuRuntimeStatusCardProps) {
  const { t } = useTranslation();
  const tone = statusTone(status);

  const rowStyle: React.CSSProperties = {
    display: 'flex',
    justifyContent: 'space-between',
    gap: '12px',
    fontSize: '12px',
    color: 'var(--text-secondary)',
  };

  return (
    <div className="card" style={{ padding: compact ? '10px 12px' : '12px 14px', marginBottom: compact ? 0 : '12px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
        <div style={{ fontSize: '13px', fontWeight: 600 }}>
          {t('feishu.runtime.title', 'Feishu Runtime Status')}
        </div>
        <span
          style={{
            fontSize: '11px',
            fontWeight: 600,
            color: tone.color,
            background: `${tone.color}22`,
            borderRadius: '999px',
            padding: '2px 8px',
          }}
        >
          {t(`feishu.runtime.${tone.key}`, tone.fallback)}
        </span>
      </div>
      <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '10px', lineHeight: 1.5 }}>
        {isAdmin ? status.message : (
          status.docs_read_ready && status.base_tasks_ready
            ? t('feishu.runtime.allReady', 'All Feishu office capabilities are available.')
            : status.docs_read_ready
              ? t('feishu.runtime.docsOnly', 'Docs / Wiki / Sheets are available. Base / Tasks are not enabled on this platform.')
              : t('feishu.runtime.notAvailable', 'Feishu office capabilities are not available on this platform.')
        )}
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
        {isAdmin ? (
          <>
            <div style={rowStyle}>
              <span>{t('feishu.runtime.cliBinary', 'CLI Binary')}</span>
              <code>{status.cli_bin || 'lark-cli'}</code>
            </div>
            <div style={rowStyle}>
              <span>{t('feishu.runtime.cliEnabled', 'CLI Enabled')}</span>
              <span>{t(`feishu.runtime.${boolKey(status.cli_enabled, 'Enabled', 'Disabled')}`, status.cli_enabled ? 'Enabled' : 'Disabled')}</span>
            </div>
            <div style={rowStyle}>
              <span>{t('feishu.runtime.cliAuth', 'CLI Auth')}</span>
              <span>{t(`feishu.runtime.${boolKey(status.cli_available, 'Ready', 'NeedsAttention')}`, status.cli_available ? 'Ready' : 'Needs Attention')}</span>
            </div>
          </>
        ) : null}
        {status.scope === 'agent' && (
          <div style={rowStyle}>
            <span>{t('feishu.runtime.channelAuth', 'Channel Auth')}</span>
            <span>{t(`feishu.runtime.${boolKey(Boolean(status.channel_configured), 'Configured', 'Missing')}`, Boolean(status.channel_configured) ? 'Configured' : 'Missing')}</span>
          </div>
        )}
        <div style={rowStyle}>
          <span>{t('feishu.runtime.docsReady', 'Docs / Wiki / Sheets')}</span>
          <span>{t(`feishu.runtime.${boolKey(status.docs_read_ready, 'Ready', 'NeedsAttention')}`, status.docs_read_ready ? 'Ready' : 'Needs Attention')}</span>
        </div>
        <div style={rowStyle}>
          <span>{t('feishu.runtime.baseReady', 'Base / Tasks')}</span>
          <span>{t(`feishu.runtime.${boolKey(status.base_tasks_ready, 'Ready', 'NeedsAttention')}`, status.base_tasks_ready ? 'Ready' : 'Needs Attention')}</span>
        </div>
      </div>
    </div>
  );
}

export default FeishuRuntimeStatusCard;
