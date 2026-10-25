import React from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';

import { agentApi } from '../../api/domains/agents';
import { AGENT_TAB_LABELS, AGENT_WORKBENCH_AREAS, isLocalAgentRuntimeType, type AgentDetailTab } from './agentDetailPolicy';
import EmployeeRoleEditor from '../../components/EmployeeRoleEditor';

interface AgentDetailHeaderProps {
  agent: any;
  agentId: string;
  canManage: boolean;
  isOperatorOnly: boolean;
  isSystemHr: boolean;
  onError: (message: string) => void;
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
  onError,
  onSelectChat,
  statusKey,
}: AgentDetailHeaderProps) {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const [editingRole, setEditingRole] = React.useState(false);
  const [editingName, setEditingName] = React.useState(false);
  const [nameInput, setNameInput] = React.useState('');
  const invalidateAgent = () => queryClient.invalidateQueries({ queryKey: ['agent', agentId] });

  // Expiry editor modal state
  const [showExpiryModal, setShowExpiryModal] = React.useState(false);
  const [expiryValue, setExpiryValue] = React.useState('');       // datetime-local string or ''
  const [expirySaving, setExpirySaving] = React.useState(false);

  const openExpiryModal = () => {
    const cur = (agent as any)?.expires_at;
    // Convert ISO to datetime-local format (YYYY-MM-DDTHH:MM)
    setExpiryValue(cur ? new Date(cur).toISOString().slice(0, 16) : '');
    setShowExpiryModal(true);
  };

  const addHours = (h: number) => {
    const base = (agent as any)?.expires_at ? new Date((agent as any).expires_at) : new Date();
    const next = new Date(base.getTime() + h * 3600_000);
    setExpiryValue(next.toISOString().slice(0, 16));
  };

  const saveExpiry = async (permanent = false) => {
    setExpirySaving(true);
    try {
      const body = permanent ? { expires_at: null } : { expires_at: expiryValue ? new Date(expiryValue).toISOString() : null };
      await agentApi.update(agentId, body as any);
      queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
      setShowExpiryModal(false);
    } catch (e) { onError(`Failed: ${e}`); }
    setExpirySaving(false);
  };

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
    <>
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
            <span
              title={agent.role_description || t('agent.roleSettings.title')}
              onClick={() => { if (canManage) setEditingRole(true); }}
              role={canManage ? 'button' : undefined}
              tabIndex={canManage ? 0 : undefined}
              onKeyDown={(event) => {
                if (canManage && (event.key === 'Enter' || event.key === ' ')) {
                  event.preventDefault(); setEditingRole(true);
                }
              }}
              className={`agent-detail-role${canManage ? ' is-editable' : ''}`}
            >
              {agent.role_description ? `· ${agent.role_description}` : canManage
                ? <span className="agent-detail-role-placeholder">· {t('agent.fields.role', 'Click to add a description...')}</span> : null}
            </span>
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
                onClick={openExpiryModal}
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
    {canManage && editingRole && <div className="card">
      <EmployeeRoleEditor key={agentId} agentId={agentId}
            value={{ role_description: agent.role_description || '', role_revision: agent.role_revision || '' }}
            onSaved={(saved) => {
              queryClient.setQueryData(['agent', agentId], (current: any) => current ? { ...current, ...saved } : current);
              setEditingRole(false); return invalidateAgent();
            }} onCancel={() => setEditingRole(false)} />
    </div>}
    {/* ── Expiry Editor Modal (admin only) ── */}
    {
      showExpiryModal && (
        <div className="ui-modal-overlay"
          onClick={() => setShowExpiryModal(false)}>
          <div className="agent-detail-modal"
            onClick={e => e.stopPropagation()}>
            <div className="agent-detail-modal-header">
              <h3 className="agent-detail-modal-title">⏰ {t('agent.settings.expiry.title')}</h3>
              <button onClick={() => setShowExpiryModal(false)} className="agent-detail-modal-close">×</button>
            </div>
            <div className="agent-detail-expiry-status">
              {(agent as any).is_expired
                ? <span className="agent-detail-expiry-status-expired">⏰ {t('agent.settings.expiry.expired')}</span>
                : (agent as any).expires_at
                  ? <>{t('agent.settings.expiry.currentExpiry')} <strong>{new Date((agent as any).expires_at).toLocaleString(i18n.language === 'zh' ? 'zh-CN' : 'en-US')}</strong></>
                  : <span className="agent-detail-expiry-status-never">{t('agent.settings.expiry.neverExpires')}</span>
              }
            </div>
            <div className="agent-detail-expiry-field">
              <div className="agent-detail-expiry-hint">{t('agent.settings.expiry.quickRenew')}</div>
              <div className="agent-detail-expiry-quick">
                {([
                  ['+ 24h', 24],
                  [`+ ${t('agent.settings.expiry.days', { count: 7 })}`, 168],
                  [`+ ${t('agent.settings.expiry.days', { count: 30 })}`, 720],
                  [`+ ${t('agent.settings.expiry.days', { count: 90 })}`, 2160],
                ] as [string, number][]).map(([label, h]) => (
                  <button key={h} onClick={() => addHours(h)}
                    className="agent-detail-expiry-quick-btn">
                    {label}
                  </button>
                ))}
              </div>
            </div>
            <div className="agent-detail-expiry-field-lg">
              <div className="agent-detail-expiry-hint">{t('agent.settings.expiry.customDeadline')}</div>
              <input type="datetime-local" value={expiryValue} onChange={e => setExpiryValue(e.target.value)}
                className="agent-detail-expiry-input" />
            </div>
            <div className="agent-detail-expiry-footer">
              <button onClick={() => saveExpiry(true)} disabled={expirySaving}
                className="agent-detail-expiry-btn agent-detail-expiry-btn-reset">
                🔓 {t('agent.settings.expiry.neverExpires')}
              </button>
              <div className="agent-detail-expiry-actions">
                <button onClick={() => setShowExpiryModal(false)} disabled={expirySaving}
                  className="agent-detail-expiry-btn agent-detail-expiry-btn-cancel">
                  {t('common.cancel')}
                </button>
                <button onClick={() => saveExpiry(false)} disabled={expirySaving || !expiryValue}
                  className="btn btn-primary"
                  style={{ opacity: !expiryValue ? 0.5 : 1 }}>
                  {expirySaving ? t('agent.settings.expiry.saving') : t('common.save')}
                </button>
              </div>
            </div>
          </div>
        </div>
      )
    }
    </>
  );
}

export function AgentDetailNavigation({ visibleTabs, activeTab, onSelectTab }: {
  visibleTabs: AgentDetailTab[];
  activeTab: string;
  onSelectTab: (tab: string) => void;
}) {
  const { t } = useTranslation();
  const visibleWorkbenchAreas = AGENT_WORKBENCH_AREAS
    .map((area) => ({
      ...area,
      tabs: area.tabs.filter((tab) => visibleTabs.includes(tab)),
    }))
    .filter((area) => area.tabs.length > 0);
  const activeWorkbenchArea =
    visibleWorkbenchAreas.find((area) => area.tabs.includes(activeTab as AgentDetailTab)) ||
    visibleWorkbenchAreas[0];
  return (
    <div className="agent-workbench-nav" data-testid="agent-workbench-nav">
      <div className="agent-workbench-areas" role="tablist" aria-label={t('agent.workbench.title', 'Agent workbench')}>
        {visibleWorkbenchAreas.map((area) => (
          <button
            key={area.id}
            type="button"
            className={`agent-workbench-area ${activeWorkbenchArea?.id === area.id ? 'active' : ''}`}
            onClick={() => onSelectTab(area.tabs.includes(area.primaryTab) ? area.primaryTab : area.tabs[0])}
          >
            {t(area.labelKey, area.fallback)}
          </button>
        ))}
      </div>
      {activeWorkbenchArea && (
        <div className="agent-workbench-subnav" aria-label={t('agent.workbench.sectionTabs', 'Section tabs')}>
          {activeWorkbenchArea.tabs.map((tab) => (
            <button
              key={tab}
              type="button"
              className={`agent-workbench-subtab ${activeTab === tab ? 'active' : ''}`}
              onClick={() => onSelectTab(tab)}
            >
              {t(`agent.tabs.${tab}`, AGENT_TAB_LABELS[tab])}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function AgentCreatedNotice({ agentId, onDismiss }: { agentId: string; onDismiss: () => void }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  return (
    <div className="agent-detail-created-banner">
      <span className="agent-detail-created-icon">&#x2705;</span>
      <div>
        <div className="agent-detail-created-title">{t('hrChat.created', 'Digital employee created successfully!')}</div>
        <div className="agent-detail-created-desc">{t('hrChat.createdDesc', 'You can now visit the detail page to further customize or start chatting.')}</div>
      </div>
      <button className="btn btn-primary agent-detail-created-action" onClick={() => navigate(`/agents/${agentId}`)}>
        {t('hrChat.goToAgent', 'Go to Detail Page')}
      </button>
      <button className="agent-detail-created-close" onClick={onDismiss}>&#x2715;</button>
    </div>
  );
}
