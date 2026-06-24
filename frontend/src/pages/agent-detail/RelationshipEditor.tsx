import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { put } from '../../api/core';
import { relationshipsApi } from '../../api/domains/relationships';
import { usersApi } from '../../api/domains/users';
import { requestAppConfirm, showAppToast } from '../../components/AppDialogs';
import { useAuthStore } from '../../stores';

type RelationshipEditorProps = {
  agentId: string;
  agent?: any;
  readOnly?: boolean;
};

export default function RelationshipEditor({ agentId, agent, readOnly = false }: RelationshipEditorProps) {
  const { t } = useTranslation();
  const qc = useQueryClient();

  const tenantId = localStorage.getItem('current_tenant_id') || '';
  const { data: a2aCollaborators } = useQuery({
    queryKey: ['a2a-collaborators', agentId],
    queryFn: () => relationshipsApi.listA2ACollaborators(agentId),
    enabled: !!agentId,
  });

  const currentUser = useAuthStore((s) => s.user);
  const { data: fetchedUsers = [] } = useQuery({
    queryKey: ['users', tenantId],
    queryFn: () => usersApi.list(tenantId) as Promise<any[]>,
    enabled: !!tenantId && !readOnly,
  });
  // If user list is empty (member 403), at least include the current user
  const users = fetchedUsers.length > 0 ? fetchedUsers
    : currentUser ? [{ id: currentUser.id, display_name: currentUser.display_name || currentUser.username, username: currentUser.username, email: currentUser.email || '' }]
    : [];

  const sameOwnerAgents = a2aCollaborators?.same_owner_agents || [];
  const collaborationGroups = a2aCollaborators?.collaboration_groups || [];

  const [binding, setBinding] = useState(false);
  const [showPicker, setShowPicker] = useState(false);

  const ownerUser = agent?.owner_user_id
    ? users.find((u: any) => u.id === agent.owner_user_id)
      || (agent.owner_username ? { display_name: agent.owner_username, username: agent.owner_username, email: '' } : null)
    : null;

  const handleBind = async (userId: string) => {
    if (readOnly) return;
    setBinding(true);
    try {
      await put(`/agents/${agentId}/owner`, { owner_user_id: userId });
      qc.invalidateQueries({ queryKey: ['agent', agentId] });
      setShowPicker(false);
    } catch (e: any) {
      showAppToast(e.message || 'Failed', 'error');
    }
    setBinding(false);
  };

  const handleUnbind = async () => {
    if (readOnly) return;
    const confirmed = await requestAppConfirm({
      title: t('agent.relationships.unbind', 'Unbind'),
      message: t('agent.relationships.confirmUnbind'),
      confirmLabel: t('agent.relationships.unbind', 'Unbind'),
      danger: true,
    });
    if (!confirmed) return;
    setBinding(true);
    try {
      await put(`/agents/${agentId}/owner`, { owner_user_id: null });
      qc.invalidateQueries({ queryKey: ['agent', agentId] });
    } catch (e: any) {
      showAppToast(e.message || 'Failed', 'error');
    }
    setBinding(false);
  };

  return (
    <div>
      {/* Owner Info + Bind */}
      <div className="card" style={{ marginBottom: '16px' }}>
        <h4 style={{ marginBottom: '8px' }}>{t('agent.relationships.owner')}</h4>
        {agent?.owner_user_id && ownerUser ? (
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px', padding: '8px 0' }}>
            <div style={{ width: '36px', height: '36px', borderRadius: '50%', background: 'var(--accent)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#fff', fontWeight: 600, fontSize: '14px' }}>
              {ownerUser.display_name?.charAt(0) || ownerUser.username?.charAt(0) || 'U'}
            </div>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: '13px', fontWeight: 500 }}>
                {ownerUser.display_name || ownerUser.username}
              </div>
              <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                {ownerUser.email} &middot; {t('agent.relationships.tokenCountOwner')}
              </div>
            </div>
            {!readOnly && (
              <button className="btn btn-ghost" style={{ fontSize: '12px', color: 'var(--error)' }} onClick={handleUnbind} disabled={binding}>
                {t('agent.relationships.unbind')}
              </button>
            )}
          </div>
        ) : agent?.owner_user_id ? (
          <div style={{ fontSize: '12px', color: 'var(--text-secondary)', padding: '8px 0' }}>
            {t('agent.relationships.boundTo')} (ID: {agent.owner_user_id})
            {!readOnly && (
              <button className="btn btn-ghost" style={{ fontSize: '12px', color: 'var(--error)', marginLeft: '8px' }} onClick={handleUnbind} disabled={binding}>
                {t('agent.relationships.unbind')}
              </button>
            )}
          </div>
        ) : (
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px', padding: '8px 0' }}>
            <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', flex: 1 }}>
              {t('agent.relationships.noOwner')}
            </div>
            {!readOnly && (
              <button className="btn btn-primary" style={{ fontSize: '12px', padding: '4px 12px' }} onClick={() => setShowPicker(true)} disabled={binding}>
                {t('agent.relationships.bindEmployee')}
              </button>
            )}
          </div>
        )}

        {/* User picker */}
        {showPicker && !readOnly && (
          <div style={{ marginTop: '12px', padding: '12px', borderRadius: '8px', background: 'var(--bg-secondary)', border: '1px solid var(--border-subtle)' }}>
            <div style={{ fontSize: '12px', fontWeight: 500, marginBottom: '8px' }}>
              {t('agent.relationships.selectEmployee')}
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', maxHeight: '200px', overflow: 'auto' }}>
              {users.map((u: any) => (
                <button
                  key={u.id}
                  onClick={() => handleBind(u.id)}
                  disabled={binding}
                  style={{
                    display: 'flex', alignItems: 'center', gap: '8px', padding: '6px 8px',
                    borderRadius: '4px', border: 'none', background: 'transparent', cursor: 'pointer',
                    textAlign: 'left', width: '100%', fontSize: '12px',
                  }}
                  onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--bg-tertiary)')}
                  onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
                >
                  <div style={{ width: '24px', height: '24px', borderRadius: '50%', background: 'var(--accent)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#fff', fontSize: '10px', fontWeight: 600 }}>
                    {u.display_name?.charAt(0) || u.username?.charAt(0)}
                  </div>
                  <div>
                    <div style={{ fontWeight: 500 }}>{u.display_name || u.username}</div>
                    <div style={{ fontSize: '10px', color: 'var(--text-tertiary)' }}>{u.email}</div>
                  </div>
                </button>
              ))}
              {users.length === 0 && (
                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', padding: '8px' }}>
                  {t('agent.relationships.noEmployees')}
                </div>
              )}
            </div>
            <button className="btn btn-ghost" style={{ fontSize: '11px', marginTop: '8px' }} onClick={() => setShowPicker(false)}>
              {t('agent.relationships.cancel')}
            </button>
          </div>
        )}
      </div>

      {/* Governed A2A collaborators */}
      <div className="card">
        <h4 style={{ marginBottom: '4px' }}>{t('agent.relationships.sameOwnerAgents', 'sameOwnerAgents')}</h4>
        <p style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
          {t('agent.relationships.sameOwnerAgentsDesc', 'Agents owned by the same user can collaborate directly.')}
        </p>
        {sameOwnerAgents.length > 0 ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {sameOwnerAgents.map((peer: any) => (
              <div key={peer.id} style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '8px', borderRadius: '6px', background: 'var(--bg-secondary)' }}>
                <div style={{ width: '28px', height: '28px', borderRadius: '50%', background: 'var(--bg-tertiary)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '12px', fontWeight: 600, color: 'var(--text-secondary)' }}>
                  {peer.name?.charAt(0) || 'A'}
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: '13px', fontWeight: 500 }}>{peer.name}</div>
                  <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{peer.role_description || t('agent.relationships.noDescription')}</div>
                </div>
                <div style={{ fontSize: '10px', padding: '2px 6px', borderRadius: '4px', background: 'var(--accent-muted)', color: 'var(--accent)' }}>
                  {t('agent.relationships.sameOwnerBadge', 'same owner')}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', padding: '8px 0' }}>
            {t('agent.relationships.noSameOwnerAgents', 'No same-owner collaborators.')}
          </div>
        )}

        <h4 style={{ margin: '18px 0 4px' }}>{t('agent.relationships.collaborationGroups', 'A2A Collaboration Groups')}</h4>
        <p style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
          {t('agent.relationships.collaborationGroupsDesc', 'Cross-owner A2A requires an approved collaboration group.')}
        </p>
        {collaborationGroups.length > 0 ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
            {collaborationGroups.map((group: any) => {
              const groupId = group.group_id || group.id || group.name;
              const groupName = group.group_name || group.name || t('agent.relationships.unnamedGroup', 'Unnamed group');
              return (
              <div key={groupId} style={{ padding: '10px', borderRadius: '6px', border: '1px solid var(--border-subtle)', background: 'var(--bg-secondary)' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', gap: '10px', marginBottom: '8px' }}>
                  <div>
                    <div style={{ fontSize: '13px', fontWeight: 600 }}>{groupName}</div>
                    {group.purpose && (
                      <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{group.purpose}</div>
                    )}
                  </div>
                  <div style={{ fontSize: '10px', color: 'var(--text-tertiary)' }}>{group.status}</div>
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                  {(group.members || []).map((member: any) => (
                    <div key={member.agent_id || member.id} style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                      <div style={{ width: '22px', height: '22px', borderRadius: '50%', background: 'var(--bg-tertiary)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '10px', fontWeight: 600 }}>
                        {member.name?.charAt(0) || 'A'}
                      </div>
                      <div style={{ minWidth: 0 }}>
                        <div style={{ fontSize: '12px', fontWeight: 500 }}>{member.name}</div>
                        <div style={{ fontSize: '10px', color: 'var(--text-tertiary)' }}>
                          {member.role_description || t('agent.relationships.noDescription')}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
              );
            })}
          </div>
        ) : (
          <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', padding: '8px 0' }}>
            {t('agent.relationships.noCollaborationGroups', 'No approved A2A collaboration groups.')}
          </div>
        )}
      </div>
    </div>
  );
}
