import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { a2aApi, type A2ACollaboratorAgent, type A2ACollaborationGroup } from '../../api/domains/a2a';

type AgentA2ASectionProps = {
  agentId: string;
};

function AgentRow({
  agent,
  badge,
  noDescription,
}: {
  agent: A2ACollaboratorAgent;
  badge: string;
  noDescription: string;
}) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '8px', borderRadius: '6px', background: 'var(--bg-secondary)' }}>
      <div style={{ width: '28px', height: '28px', borderRadius: '50%', background: 'var(--bg-tertiary)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '12px', fontWeight: 600, color: 'var(--text-secondary)' }}>
        {agent.name?.charAt(0) || 'A'}
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: '13px', fontWeight: 500 }}>{agent.name}</div>
        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
          {agent.role_description || noDescription}
        </div>
      </div>
      <div style={{ fontSize: '10px', padding: '2px 6px', borderRadius: '4px', background: 'var(--accent-muted)', color: 'var(--accent)', whiteSpace: 'nowrap' }}>
        {badge}
      </div>
    </div>
  );
}

function AgentList({
  title,
  description,
  badge,
  agents,
  empty,
  noDescription,
}: {
  title: string;
  description: string;
  badge: string;
  agents: A2ACollaboratorAgent[];
  empty: string;
  noDescription: string;
}) {
  return (
    <section style={{ marginBottom: '18px' }}>
      <h4 style={{ marginBottom: '4px' }}>{title}</h4>
      <p style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>{description}</p>
      {agents.length > 0 ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
          {agents.map((agent) => (
            <AgentRow key={agent.id} agent={agent} badge={badge} noDescription={noDescription} />
          ))}
        </div>
      ) : (
        <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', padding: '8px 0' }}>{empty}</div>
      )}
    </section>
  );
}

function GroupList({ groups }: { groups: A2ACollaborationGroup[] }) {
  const { t } = useTranslation();
  if (groups.length === 0) {
    return (
      <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', padding: '8px 0' }}>
        {t('agent.a2a.noCollaborationGroups', 'No approved A2A collaboration groups.')}
      </div>
    );
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
      {groups.map((group) => {
        const groupId = group.group_id;
        const groupName = group.group_name || t('agent.a2a.unnamedGroup', 'Unnamed group');
        return (
          <div key={groupId} style={{ padding: '10px', borderRadius: '6px', border: '1px solid var(--border-subtle)', background: 'var(--bg-secondary)' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', gap: '10px', marginBottom: '8px' }}>
              <div>
                <div style={{ fontSize: '13px', fontWeight: 600 }}>{groupName}</div>
                {group.purpose && <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{group.purpose}</div>}
              </div>
              <div style={{ fontSize: '10px', color: 'var(--text-tertiary)' }}>{group.status}</div>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
              {(group.members || []).map((member) => (
                <div key={member.agent_id || member.id} style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                  <div style={{ width: '22px', height: '22px', borderRadius: '50%', background: 'var(--bg-tertiary)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '10px', fontWeight: 600 }}>
                    {member.name?.charAt(0) || 'A'}
                  </div>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontSize: '12px', fontWeight: 500 }}>{member.name}</div>
                    <div style={{ fontSize: '10px', color: 'var(--text-tertiary)' }}>
                      {member.role_description || t('agent.a2a.noDescription', 'No description')}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}

export default function AgentA2ASection({ agentId }: AgentA2ASectionProps) {
  const { t } = useTranslation();
  const { data, isLoading, isError } = useQuery({
    queryKey: ['a2a-collaborators', agentId],
    queryFn: () => a2aApi.listCollaborators(agentId),
    enabled: !!agentId,
  });

  const sameOwnerAgents = data?.same_owner_agents || [];
  const publicAgents = data?.public_agents || [];
  const collaborationGroups = data?.collaboration_groups || [];

  if (isLoading) {
    return <div className="card">{t('common.loading', 'Loading...')}</div>;
  }
  if (isError) {
    return <div className="card">{t('agent.a2a.loadFailed', 'Failed to load A2A collaborators.')}</div>;
  }

  return (
    <div className="card">
      <AgentList
        title={t('agent.a2a.sameOwnerAgents', 'Same-owner agents')}
        description={t('agent.a2a.sameOwnerAgentsDesc', 'Agents owned by the same user can collaborate directly.')}
        badge={t('agent.a2a.sameOwnerBadge', 'same owner')}
        agents={sameOwnerAgents}
        empty={t('agent.a2a.noSameOwnerAgents', 'No same-owner collaborators.')}
        noDescription={t('agent.a2a.noDescription', 'No description')}
      />
      <AgentList
        title={t('agent.a2a.publicAgents', 'Public agents')}
        description={t('agent.a2a.publicAgentsDesc', 'Public same-tenant agents can collaborate directly while they remain public.')}
        badge={t('agent.a2a.publicBadge', 'public')}
        agents={publicAgents}
        empty={t('agent.a2a.noPublicAgents', 'No public collaborators.')}
        noDescription={t('agent.a2a.noDescription', 'No description')}
      />
      <section>
        <h4 style={{ margin: '18px 0 4px' }}>{t('agent.a2a.collaborationGroups', 'A2A Collaboration Groups')}</h4>
        <p style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
          {t('agent.a2a.collaborationGroupsDesc', 'Cross-owner private A2A requires an approved collaboration group.')}
        </p>
        <GroupList groups={collaborationGroups} />
      </section>
    </div>
  );
}
