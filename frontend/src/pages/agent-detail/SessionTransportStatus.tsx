import { useTranslation } from 'react-i18next';

import type { ChatTransportPhase } from './chatTransportRecovery';
import { RuntimeBudgetNotice } from './RuntimeBudgetNotice';
import type { RuntimeBudgetState } from './runtimeBudgetState';

export interface SessionTransportStatusProps {
  phase: ChatTransportPhase;
  attempt?: number;
  onReconnect?: () => void;
  runtimeBudget?: RuntimeBudgetState | null;
  onReload?: () => void;
}

export function SessionTransportStatus({
  phase,
  attempt = 0,
  onReconnect,
  runtimeBudget,
  onReload = () => window.location.reload(),
}: SessionTransportStatusProps) {
  const { t } = useTranslation();

  const message = phase === 'initializing'
    ? t('agent.chat.transport.initializing', 'Loading durable session history...')
    : phase === 'offline'
    ? t(
        'agent.chat.transport.offline',
        'You are offline. The task is still running in the background and durable history will catch up automatically when the network returns.',
      )
    : phase === 'auth_failed'
      ? t(
          'agent.chat.transport.authFailed',
          'Live updates could not be authorized. Sign in again to restore this session.',
        )
      : phase === 'degraded'
        ? t(
            'agent.chat.transport.degraded',
            'Live updates are unavailable. The task is still running in the background; this view is recovering from durable history.',
          )
        : t('agent.chat.transport.reconnecting', 'Live updates reconnecting...');
  const canReconnect = Boolean(onReconnect && (phase === 'reconnecting' || phase === 'degraded'));

  return (
    <>
      <RuntimeBudgetNotice runtimeBudget={runtimeBudget} />
      {phase === 'connected' ? null : (
        <div
          role="status"
          aria-live="polite"
          data-testid="session-transport-status"
          data-transport-phase={phase}
          data-reconnect-attempt={attempt || undefined}
          style={{
            padding: '6px 16px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: '10px',
            fontSize: '11px',
            color: 'var(--text-tertiary)',
            borderTop: '1px solid var(--border-subtle)',
          }}
        >
          <span style={{ display: 'flex', alignItems: 'center', gap: '7px' }}>
            <span
              aria-hidden="true"
              style={{
                display: 'inline-block',
                width: '5px',
                height: '5px',
                borderRadius: '50%',
                background: phase === 'auth_failed' ? 'var(--color-danger)' : 'var(--accent-primary)',
                opacity: 0.8,
              }}
            />
            {message}
          </span>
          {canReconnect ? (
            <button
              type="button"
              className="btn btn-ghost"
              data-testid="session-transport-reconnect"
              onClick={onReconnect}
            >
              {t('agent.chat.transport.retryNow', 'Retry now')}
            </button>
          ) : null}
          {phase === 'auth_failed' ? (
            <button
              type="button"
              className="btn btn-ghost"
              data-testid="session-transport-reload"
              onClick={onReload}
            >
              {t('agent.chat.transport.reload', 'Reload')}
            </button>
          ) : null}
        </div>
      )}
    </>
  );
}
