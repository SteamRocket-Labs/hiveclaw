import { runtimeStatusLabel } from '../agent-detail/runtimeStatus';
import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useInRouterContext, useNavigate } from 'react-router-dom';
import { IconChevronDown, IconPlus } from '@tabler/icons-react';

import type { SessionWorkbenchHeaderModel } from './timelineModel';
import { sessionTitleForUser } from './sessionTitlePresentation';
import { buildNewSessionDraftNavigation } from '../agent-detail/newSessionNavigation';
import './SessionWorkbenchChrome.css';

function statusColor(status: SessionWorkbenchHeaderModel['status']): string {
  if (status === 'running' || status === 'streaming') return 'var(--accent-primary)';
  if (status === 'waiting') return 'var(--warning)';
  if (status === 'failed') return 'var(--error)';
  return 'var(--text-tertiary)';
}

function historyTime(session: Record<string, unknown>, locale: string): string {
  const raw = session.last_message_at || session.updated_at || session.created_at;
  if (!raw) return '';
  try {
    return new Date(String(raw)).toLocaleDateString(locale, { month: 'short', day: 'numeric' });
  } catch {
    return '';
  }
}

/**
 * A stop-acknowledgement notice describes a run that is still settling. Once
 * the run itself has reached its terminal state (已停止 and friends), the
 * notice is stale and must not linger above the composer (B2). Other notices
 * are untouched.
 */
export function settledRunHidesNotice(
  notice: string | null | undefined,
  runStatus: string | null | undefined,
  stopNoticeText: string,
): boolean {
  if (!notice || notice !== stopNoticeText) return false;
  return ['cancelled', 'canceled', 'stopped', 'failed', 'complete', 'completed'].includes(String(runStatus || ''));
}

export interface SessionHistoryMenuProps {
  /** The bot whose conversations these are; also the new-conversation target. */
  agentId: string;
  /** This bot's conversations, most recent first; existing session rows only. */
  sessions: Array<Record<string, unknown>>;
  loading?: boolean;
  activeSessionId?: string | null;
  /** Management entry (B5): set only for people who can manage this bot. */
  manageHref?: string | null;
}

/** The menu routes between conversations, so it needs a Router; mounted
 * harnesses that render the chat section bare get no menu. */
function SessionHistoryMenuWithRouter(props: SessionHistoryMenuProps) {
  if (!useInRouterContext()) return null;
  return <SessionHistoryMenu {...props} />;
}

function SessionHistoryMenu({ agentId, sessions, loading, activeSessionId }: SessionHistoryMenuProps) {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const locale = i18n.language?.startsWith('zh') ? 'zh-CN' : 'en-US';

  useEffect(() => {
    if (!open) return;
    const handleClickOutside = (event: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(event.target as Node)) setOpen(false);
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', handleClickOutside);
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [open]);

  return (
    <div className="swb-history" ref={wrapRef}>
      <button
        type="button"
        className="swb-history-trigger"
        aria-expanded={open}
        aria-haspopup="listbox"
        onClick={() => setOpen((value) => !value)}
        title={t('sessionWorkbench.history', 'History')}
      >
        {t('sessionWorkbench.history', 'History')}
        <IconChevronDown size={13} stroke={1.8} style={{ transform: open ? 'rotate(180deg)' : 'none' }} />
      </button>
      {open && (
        <div className="swb-history-menu" data-testid="session-history-menu">
          <button
            type="button"
            className="swb-history-new"
            onClick={() => {
              setOpen(false);
              const request = buildNewSessionDraftNavigation(agentId);
              navigate(request.to, { state: request.state });
            }}
          >
            <IconPlus size={13} stroke={1.8} />
            {t('agent.chat.newSession', 'New Conversation')}
          </button>
          <div className="swb-history-list" role="listbox">
            {loading ? (
              <div className="swb-history-empty">{t('common.loading', 'Loading')}</div>
            ) : sessions.length === 0 ? (
              <div className="swb-history-empty">{t('agent.chat.noSessionsYet', 'No conversations yet.')}</div>
            ) : (
              sessions.map((session) => {
                const id = String(session.id ?? '');
                const isActive = Boolean(activeSessionId) && (id === String(activeSessionId)
                  || String(session.chat_session_id || '') === String(activeSessionId));
                return (
                  <Link
                    key={id}
                    to={`/agents/${agentId}?session_id=${encodeURIComponent(id)}#chat`}
                    role="option"
                    aria-selected={isActive}
                    className={`swb-history-item ${isActive ? 'active' : ''}`}
                    onClick={() => setOpen(false)}
                  >
                    <span className="swb-history-item-title">
                      {sessionTitleForUser(session, t('agent.chat.session', 'Session'))}
                    </span>
                    <span className="swb-history-item-meta">{historyTime(session, locale)}</span>
                  </Link>
                );
              })
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export function SessionWorkbenchHeader({ model, history, manageHref }: {
  model: SessionWorkbenchHeaderModel;
  /** When provided, the header offers this bot's conversations and a fresh one. */
  history?: SessionHistoryMenuProps;
  /** Management entry (B5 review): set only for people who can manage this
   * bot. Local-runtime conversations pass it directly — they have no history
   * menu, but their managers still need the way in. */
  manageHref?: string | null;
}) {
  const { t } = useTranslation();
  // The header shows only the five user-visible states (running, waiting for
  // you, done, failed, stopped). Unconfirmed-outcome sessions surface through
  // their underlying status, not a dedicated chip (product definition P3).
  // A conversation with no run yet (idle) shows no run status at all.
  // Failed runs show no "see below" line: the failure card speaks for itself
  // and a member must never be promised steps that are not there (B2).
  const showRunStatus = model.status !== 'idle';
  const statusLabel = runtimeStatusLabel(model.status, t);
  const guidance = model.status === 'waiting'
    ? t('sessionWorkbench.waitingGuidance', 'Review the request below and choose the next action.')
    : null;
  const managementHref = manageHref ?? history?.manageHref ?? null;
  return (
    <div data-testid="session-workbench-header" className="swb-header">
      <div className="swb-header-title-row">
        <h2 className="swb-header-title">{model.title || t('agent.chat.newSessionTitle', 'New conversation')}</h2>
        {history && <SessionHistoryMenuWithRouter {...history} />}
        {managementHref && (
          <Link className="swb-history-trigger" to={managementHref}>
            {t('nav.settings', 'Settings')}
          </Link>
        )}
        {(showRunStatus || model.modelLabel) && (
          <span className="swb-header-status">
            {showRunStatus && (
              <>
                <span className="swb-status-dot" style={{ background: statusColor(model.status) }} />
                <span>{statusLabel}</span>
              </>
            )}
            {model.modelLabel && <span>{showRunStatus ? `· ${model.modelLabel}` : model.modelLabel}</span>}
          </span>
        )}
      </div>
      {guidance && <div className="swb-header-guidance" role="status">{guidance}</div>}
    </div>
  );
}
