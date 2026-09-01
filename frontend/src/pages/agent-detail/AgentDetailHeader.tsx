import React from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { agentApi } from '../../api/domains/agents';
import { isLocalAgentRuntimeType } from './agentDetailPolicy';

interface AgentDetailHeaderProps {
  agent: any;
  agentId: string;
  canManage: boolean;
  isOperatorOnly: boolean;
  isSystemHr: boolean;
  onOpenExpiry: () => void;
  onSelectChat: () => void;
  statusKey: string;
}

export function AgentDetailSectionFallback() {
  return (
    <div className="agent-detail-section-fallback" role="status" aria-live="polite">
      <span className="agent-detail-section-fallback-dot" aria-hidden="true" />
      Loading workspace…
    </div>
  );
}

export function agentStatusKey(agent: { status?: string }): string {
  if (agent.status === 'error') return 'error';
  if (agent.status === 'creating') return 'creating';
  if (agent.status === 'stopped') return 'stopped';
  return agent.status === 'running' ? 'running' : 'idle';
}

export default function AgentDetailHeader({
  agent,
  agentId,
  canManage,
  isOperatorOnly,
  isSystemHr,
  onOpenExpiry,
  onSelectChat,
  statusKey,
}: AgentDetailHeaderProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [editingRole, setEditingRole] = React.useState(false);
  const [roleInput, setRoleInput] = React.useState('');
  const [editingName, setEditingName] = React.useState(false);
  const [nameInput, setNameInput] = React.useState('');
  const invalidateAgent = () => queryClient.invalidateQueries({ queryKey: ['agent', agentId] });

  if (isSystemHr) {
    return (
      <div className="page-header">
        <div className="agent-detail-hr-header">
          <div className="agent-detail-hr-avatar">&#x1F464;</div>
          <div>
            <h1 className="page-title agent-detail-title-flush">{t('nav.newAgent', 'Create Digital Employee')}</h1>
            <p className="page-subtitle agent-detail-subtitle-tight">
              {t('hrChat.subtitle', 'Tell the HR agent what kind of digital employee you need')}
            </p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="page-header">
      <div className="agent-detail-header-main">
        <div className="agent-detail-avatar">
          {(Array.from(agent.name || 'A')[0] as string || 'A').toUpperCase()}
        </div>
        <div className="agent-detail-header-text">
          {canManage && editingName ? (
            <input
              className="page-title agent-detail-name-input"
              autoFocus
              value={nameInput}
              onChange={(event) => setNameInput(event.target.value)}
              onBlur={async () => {
                setEditingName(false);
                if (nameInput.trim() && nameInput !== agent.name) {
                  await agentApi.update(agentId, { name: nameInput.trim() } as any);
                  await invalidateAgent();
                } else {
                  setNameInput(agent.name);
                }
              }}
              onKeyDown={(event) => {
                if (event.key === 'Enter') (event.target as HTMLInputElement).blur();
                if (event.key === 'Escape') {
                  setEditingName(false);
                  setNameInput(agent.name);
                }
              }}
            />
          ) : (
            <h1
              className={`page-title agent-detail-name${canManage ? ' is-editable' : ''}`}
              title={canManage ? 'Click to edit name' : undefined}
              onClick={() => {
                if (!canManage) return;
                setNameInput(agent.name);
                setEditingName(true);
              }}
            >
              {agent.name}
            </h1>
          )}
          <p className="page-subtitle agent-detail-status-line">
            <span className={`status-dot ${statusKey}`} />
            {t(`agent.status.${statusKey}`)}
            {canManage && editingRole ? (
              <textarea
                autoFocus
                value={roleInput}
                onChange={(event) => setRoleInput(event.target.value)}
                onBlur={async () => {
                  setEditingRole(false);
                  if (roleInput !== agent.role_description) {
                    await agentApi.update(agentId, { role_description: roleInput } as any);
                    await invalidateAgent();
                  }
                }}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && !event.shiftKey) {
                    event.preventDefault();
                    (event.target as HTMLTextAreaElement).blur();
                  }
                  if (event.key === 'Escape') {
                    setEditingRole(false);
                    setRoleInput(agent.role_description || '');
                  }
                }}
                rows={2}
                className="agent-detail-role-input"
              />
            ) : (
              <span
                title={canManage ? (agent.role_description || 'Click to edit') : (agent.role_description || '')}
                onClick={() => {
                  if (!canManage) return;
                  setRoleInput(agent.role_description || '');
                  setEditingRole(true);
                }}
                className={`agent-detail-role${canManage ? ' is-editable' : ''}`}
              >
                {agent.role_description
                  ? `· ${agent.role_description}`
                  : canManage
                    ? <span className="agent-detail-role-placeholder">· {t('agent.fields.role', 'Click to add a description...')}</span>
                    : null}
              </span>
            )}
            {agent.is_expired && (
              <span className="agent-detail-expired-badge">{t('agent.settings.expiry.expired', 'Expired')}</span>
            )}
            {agent.agent_type === 'local_agent' && (
              <span className="agent-detail-local-badge">{t('nav.localBadge', 'Local')}</span>
            )}
            {!agent.is_expired && agent.expires_at && (
              <span className="agent-detail-expires-text">
                {t('agent.settings.expiry.expiresAt', 'Expires: {{time}}', {
                  time: new Date(agent.expires_at).toLocaleString(),
                })}
              </span>
            )}
            {canManage && (
              <button
                onClick={onOpenExpiry}
                title={t('agent.settings.expiry.editTitle', 'Edit expiry time')}
                className="agent-detail-expiry-edit"
              >
                ✏️ {t(agent.expires_at || agent.is_expired ? 'agent.settings.expiry.renew' : 'agent.settings.expiry.setExpiry')}
              </button>
            )}
          </p>
        </div>
      </div>
      {!isOperatorOnly && (
        <div className="agent-detail-header-actions">
          <button className="btn btn-primary" onClick={onSelectChat}>{t('agent.actions.chat')}</button>
          {canManage && !isLocalAgentRuntimeType(agent) && (
            <>
              {agent.status === 'stopped' ? (
                <button className="btn btn-secondary" onClick={async () => {
                  await agentApi.start(agentId);
                  await invalidateAgent();
                }}>
                  {t('agent.actions.start')}
                </button>
              ) : agent.status === 'running' ? (
                <button className="btn btn-secondary" onClick={async () => {
                  await agentApi.stop(agentId);
                  await invalidateAgent();
                }}>
                  {t('agent.actions.stop')}
                </button>
              ) : null}
            </>
          )}
        </div>
      )}
    </div>
  );
}
