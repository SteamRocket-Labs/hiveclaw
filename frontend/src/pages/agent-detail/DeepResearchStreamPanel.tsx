/**
 * Tier 3-4 UI: live progress panel for a running Deep Research task.
 *
 * Subscribes to `useDeepResearchStream` and renders the incremental state
 * (status / source count / claim count / latest reflection / report preview)
 * with a one-click abort. Mounted by `StructuredToolResultBody` whenever a
 * `deep_research_*` tool result carries `status === 'running'` and a task_id.
 */

import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';

import { useDeepResearchStream } from '../../hooks/useDeepResearchStream';

interface DeepResearchStreamPanelProps {
  agentId: string;
  taskId: string;
}

const _STATUS_STYLES: Record<string, { background: string; color: string; label: string }> = {
  idle: { background: 'rgba(120, 120, 120, 0.18)', color: 'var(--text-secondary)', label: 'Idle' },
  streaming: { background: 'rgba(80, 140, 220, 0.22)', color: '#7aa8e6', label: 'Streaming' },
  completed: { background: 'rgba(80, 200, 130, 0.22)', color: '#7ad3a0', label: 'Completed' },
  failed: { background: 'rgba(220, 100, 100, 0.22)', color: '#e68080', label: 'Failed' },
  aborted: { background: 'rgba(220, 180, 80, 0.22)', color: '#e6c073', label: 'Aborted' },
};

export default function DeepResearchStreamPanel({ agentId, taskId }: DeepResearchStreamPanelProps) {
  const { t } = useTranslation();
  const stream = useDeepResearchStream(agentId, taskId, { pollInterval: 0.5, deadlineSeconds: 1800 });

  const statusStyle = _STATUS_STYLES[stream.status] ?? _STATUS_STYLES.idle;
  const latestReflection = useMemo(() => {
    if (stream.reflections.length === 0) return null;
    return stream.reflections[stream.reflections.length - 1];
  }, [stream.reflections]);
  const latestControllerStep = useMemo(() => {
    if (stream.controllerTrace.length === 0) return null;
    return stream.controllerTrace[stream.controllerTrace.length - 1];
  }, [stream.controllerTrace]);

  const reportPreview = stream.reportMarkdown.length > 1200
    ? stream.reportMarkdown.slice(-1200)
    : stream.reportMarkdown;

  return (
    <div
      data-testid="deep-research-stream-panel"
      style={{
        display: 'grid',
        gap: '8px',
        padding: '8px 10px',
        borderRadius: '6px',
        background: 'rgba(255, 255, 255, 0.03)',
        border: '1px solid rgba(255, 255, 255, 0.06)',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
        <span
          style={{
            fontSize: '10px',
            fontWeight: 700,
            padding: '2px 8px',
            borderRadius: '999px',
            background: statusStyle.background,
            color: statusStyle.color,
            letterSpacing: '0.4px',
            textTransform: 'uppercase',
          }}
        >
          {t(`agent.chat.deepResearchStream.status.${stream.status}`, statusStyle.label)}
        </span>
        <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
          {t('agent.chat.deepResearchStream.taskLabel', 'Task')}: <code>{taskId.slice(0, 8)}…</code>
        </span>
        {stream.status === 'streaming' && (
          <button
            type="button"
            onClick={stream.abort}
            style={{
              marginLeft: 'auto',
              fontSize: '11px',
              padding: '2px 8px',
              borderRadius: '4px',
              border: '1px solid var(--border-secondary)',
              background: 'transparent',
              color: 'var(--text-secondary)',
              cursor: 'pointer',
            }}
          >
            {t('agent.chat.deepResearchStream.abort', 'Stop stream')}
          </button>
        )}
      </div>

      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(110px, 1fr))',
          gap: '6px',
        }}
      >
        <_StatTile
          label={t('agent.chat.deepResearchStream.sources', 'Sources')}
          value={stream.sourceNotes.length}
        />
        <_StatTile
          label={t('agent.chat.deepResearchStream.claims', 'Claims')}
          value={stream.claims.length}
        />
        <_StatTile
          label={t('agent.chat.deepResearchStream.lanes', 'Lanes')}
          value={stream.laneSummaries.length}
        />
        <_StatTile
          label={t('agent.chat.deepResearchStream.steps', 'Steps')}
          value={stream.steps.length}
        />
        <_StatTile
          label={t('agent.chat.deepResearchStream.heartbeats', 'Heartbeats')}
          value={stream.heartbeats}
        />
      </div>

      {latestReflection && (
        <div
          style={{
            fontSize: '11px',
            color: 'var(--text-secondary)',
            background: 'rgba(0,0,0,0.15)',
            borderRadius: '4px',
            padding: '6px 8px',
          }}
        >
          <div style={{ fontWeight: 600, color: 'var(--text-tertiary)', marginBottom: '2px' }}>
            {t('agent.chat.deepResearchStream.latestReflection', 'Latest reflection')}
          </div>
          <div style={{ whiteSpace: 'pre-wrap' }}>
            {String(latestReflection.payload.rationale ?? '—')}
          </div>
        </div>
      )}

      {latestControllerStep && (
        <div
          style={{
            fontSize: '11px',
            color: 'var(--text-secondary)',
            background: 'rgba(0,0,0,0.15)',
            borderRadius: '4px',
            padding: '6px 8px',
          }}
        >
          <div style={{ fontWeight: 600, color: 'var(--text-tertiary)', marginBottom: '2px' }}>
            {t('agent.chat.deepResearchStream.latestControllerStep', 'Controller action')}:{' '}
            <code>{String(latestControllerStep.payload.action_type ?? '')}</code>{' '}
            <span style={{ color: 'var(--text-tertiary)' }}>
              ({String(latestControllerStep.payload.role ?? '')})
            </span>
          </div>
          <div style={{ whiteSpace: 'pre-wrap' }}>
            {String(latestControllerStep.payload.rationale ?? '')}
          </div>
        </div>
      )}

      {reportPreview && (
        <details>
          <summary style={{ cursor: 'pointer', fontSize: '11px', color: 'var(--text-tertiary)' }}>
            {t('agent.chat.deepResearchStream.reportPreview', 'Report preview')} (
            {stream.reportMarkdown.length} chars)
          </summary>
          <pre
            style={{
              fontSize: '11px',
              fontFamily: 'var(--font-mono)',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              maxHeight: '260px',
              overflow: 'auto',
              background: 'rgba(0,0,0,0.18)',
              borderRadius: '4px',
              padding: '6px 8px',
              marginTop: '6px',
            }}
          >
            {reportPreview}
          </pre>
        </details>
      )}

      {stream.error && (
        <div
          style={{
            fontSize: '11px',
            color: '#e68080',
            background: 'rgba(220, 100, 100, 0.12)',
            borderRadius: '4px',
            padding: '6px 8px',
          }}
        >
          {t('agent.chat.deepResearchStream.errorLabel', 'Stream error')}: {stream.error.message}
        </div>
      )}
    </div>
  );
}

function _StatTile({ label, value }: { label: string; value: number }) {
  return (
    <div
      style={{
        background: 'rgba(0,0,0,0.18)',
        borderRadius: '4px',
        padding: '4px 8px',
        textAlign: 'center',
      }}
    >
      <div style={{ fontSize: '16px', fontWeight: 700, color: 'var(--text-primary)' }}>{value}</div>
      <div style={{ fontSize: '10px', color: 'var(--text-tertiary)', textTransform: 'uppercase', letterSpacing: '0.4px' }}>
        {label}
      </div>
    </div>
  );
}
