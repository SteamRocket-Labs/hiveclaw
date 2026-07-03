import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { a2aApi, type A2ACollaboratorAgent, type A2ACollaborationGroup } from '../../api/domains/a2a';
import './AgentA2ASection.css';

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
    <div className="agent-a2a-row">
      <div className="agent-a2a-avatar">
        {agent.name?.charAt(0) || 'A'}
      </div>
      <div className="agent-a2a-row-body">
        <div className="agent-a2a-name">{agent.name}</div>
        <div className="agent-a2a-meta">
          {agent.role_description || noDescription}
        </div>
      </div>
      <div className="agent-a2a-badge">
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
    <section className="agent-a2a-list">
      <h4 className="agent-a2a-heading">{title}</h4>
      <p className="agent-a2a-desc">{description}</p>
      {agents.length > 0 ? (
        <div className="agent-a2a-agents">
          {agents.map((agent) => (
            <AgentRow key={agent.id} agent={agent} badge={badge} noDescription={noDescription} />
          ))}
        </div>
      ) : (
        <div className="agent-a2a-empty">{empty}</div>
      )}
    </section>
  );
}

function GroupList({ groups }: { groups: A2ACollaborationGroup[] }) {
  const { t } = useTranslation();
  if (groups.length === 0) {
    return (
      <div className="agent-a2a-empty">
        {t('agent.a2a.noCollaborationGroups', 'No approved A2A collaboration groups.')}
      </div>
    );
  }
  return (
    <div className="agent-a2a-groups">
      {groups.map((group) => {
        const groupId = group.group_id;
        const groupName = group.group_name || t('agent.a2a.unnamedGroup', 'Unnamed group');
        return (
          <div key={groupId} className="agent-a2a-group">
            <div className="agent-a2a-group-header">
              <div>
                <div className="agent-a2a-group-name">{groupName}</div>
                {group.purpose && <div className="agent-a2a-meta">{group.purpose}</div>}
              </div>
              <div className="agent-a2a-group-status">{group.status}</div>
            </div>
            <div className="agent-a2a-members">
              {(group.members || []).map((member) => (
                <div key={member.agent_id || member.id} className="agent-a2a-member">
                  <div className="agent-a2a-member-avatar">
                    {member.name?.charAt(0) || 'A'}
                  </div>
                  <div className="agent-a2a-member-body">
                    <div className="agent-a2a-member-name">{member.name}</div>
                    <div className="agent-a2a-member-role">
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
        <h4 className="agent-a2a-heading-spaced">{t('agent.a2a.collaborationGroups', 'A2A Collaboration Groups')}</h4>
        <p className="agent-a2a-desc">
          {t('agent.a2a.collaborationGroupsDesc', 'Cross-owner private A2A requires an approved collaboration group.')}
        </p>
        <GroupList groups={collaborationGroups} />
      </section>
    </div>
  );
}
