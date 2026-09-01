import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  IconArrowRight,
  IconBrain,
  IconDeviceDesktop,
  IconMessageCircle,
  IconPlus,
  IconRefresh,
  IconRoute,
  IconSearch,
  IconUserCircle,
} from '@tabler/icons-react';
import { agentApi } from '../api/domains/agents';
import type { Agent } from '../types';
import HrCreationRecoveryPanel from './employee-directory/HrCreationRecoveryPanel';

function formatAgentStatus(status: Agent['status'] | string | undefined) {
  if (!status) return 'idle';
  return status;
}

function isLocalAgentRuntime(agent: Agent): boolean {
  return agent.agent_type === 'local_agent';
}

function isOwnedAgent(agent: Agent): boolean {
  return agent.is_owner === true;
}

function isOperatorOnlyAgent(agent: Agent): boolean {
  return agent.access_level === 'operator';
}

export default function DigitalEmployees() {
  const { t } = useTranslation();
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<'all' | 'mine' | 'shared' | 'recommended' | 'running' | 'needs_attention' | 'local'>('all');
  const { data: agents = [], isLoading } = useQuery({
    queryKey: ['agents'],
    queryFn: () => agentApi.list(),
  });

  const visibleAgents = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    return agents.filter((agent: Agent) => {
      const matchesQuery =
        !normalizedQuery ||
        agent.name.toLowerCase().includes(normalizedQuery) ||
        (agent.role_description || '').toLowerCase().includes(normalizedQuery);
      const matchesFilter =
        filter === 'all' ||
        (filter === 'mine' && isOwnedAgent(agent)) ||
        (filter === 'shared' && !isOwnedAgent(agent)) ||
        (filter === 'recommended' && agent.status === 'running' && Boolean(agent.last_active_at)) ||
        (filter === 'running' && agent.status === 'running') ||
        (filter === 'needs_attention' && ['creating', 'error', 'stopped'].includes(agent.status)) ||
        (filter === 'local' && isLocalAgentRuntime(agent));
      return matchesQuery && matchesFilter;
    });
  }, [agents, filter, query]);

  const runningCount = agents.filter((agent: Agent) => agent.status === 'running').length;
  const attentionCount = agents.filter((agent: Agent) => ['creating', 'error', 'stopped'].includes(agent.status)).length;
  const mineCount = agents.filter((agent: Agent) => isOwnedAgent(agent)).length;
  const sharedCount = agents.filter((agent: Agent) => !isOwnedAgent(agent)).length;
  const localCount = agents.filter((agent: Agent) => isLocalAgentRuntime(agent)).length;

  return (
    <div className="workbench-page">
      <div className="workbench-hero">
        <div>
          <span className="workbench-eyebrow">{t('employees.eyebrow', 'Agent control surface')}</span>
          <h1 className="page-title">{t('employees.title', 'Digital Employees')}</h1>
          <p className="page-subtitle">
            {t('employees.subtitle', 'Manage employee identity, sessions, memory, capabilities, workflow, delegation, and workspace from one directory.')}
          </p>
        </div>
        <Link to="/agents/new" className="btn btn-primary employee-directory-create">
          <IconPlus size={16} stroke={1.7} />
          {t('employees.createViaHr', 'Create via HR')}
        </Link>
      </div>

      <div className="workbench-metrics">
        <div className="workbench-metric">
          <span>{t('employees.metrics.total', 'Total')}</span>
          <strong>{agents.length}</strong>
        </div>
        <div className="workbench-metric">
          <span>{t('employees.metrics.mine', 'Owned by me')}</span>
          <strong>{mineCount}</strong>
        </div>
        <div className="workbench-metric">
          <span>{t('employees.metrics.shared', 'Company shared')}</span>
          <strong>{sharedCount}</strong>
        </div>
        <div className="workbench-metric">
          <span>{t('employees.metrics.running', 'Running')}</span>
          <strong>{runningCount}</strong>
        </div>
        <div className="workbench-metric">
          <span>{t('employees.metrics.attention', 'Needs attention')}</span>
          <strong>{attentionCount}</strong>
        </div>
        <div className="workbench-metric">
          <span>{t('employees.metrics.local', 'Local runtime')}</span>
          <strong>{localCount}</strong>
        </div>
      </div>

      <HrCreationRecoveryPanel />

      <section className="workbench-panel">
        <div className="workbench-toolbar">
          <div className="workbench-search">
            <IconSearch size={15} stroke={1.7} />
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t('employees.search', 'Search employees...')} />
          </div>
          <div className="workbench-segmented">
            {[
              ['all', t('employees.filters.all', 'All')],
              ['mine', t('employees.filters.mine', 'Owned by me')],
              ['shared', t('employees.filters.shared', 'Company shared')],
              ['recommended', t('employees.filters.recommended', 'Recommended')],
              ['running', t('employees.filters.running', 'Running')],
              ['needs_attention', t('employees.filters.attention', 'Needs attention')],
              ['local', t('employees.filters.local', 'Local runtime')],
            ].map(([value, label]) => (
              <button key={value} className={filter === value ? 'active' : ''} onClick={() => setFilter(value as typeof filter)}>
                {label}
              </button>
            ))}
          </div>
        </div>

        {isLoading && <div className="workbench-empty">{t('common.loading', 'Loading...')}</div>}
        {!isLoading && visibleAgents.length === 0 && (
          <div className="workbench-empty">{t('employees.empty', 'No employees match this view.')}</div>
        )}
        <div className="employee-grid employee-grid-flat">
          {visibleAgents.map((agent: Agent) => (
            <article key={agent.id} className="employee-card">
              <div className="employee-card-body">
                <div className="employee-card-header">
                  <span className="employee-avatar">
                    {(Array.from(agent.name || 'A')[0] as string || 'A').toUpperCase()}
                  </span>
                  <div className="employee-card-copy">
                    <div className="employee-card-title-row">
                      <h2>{agent.name}</h2>
                      <span className={`employee-status ${formatAgentStatus(agent.status)}`}>{formatAgentStatus(agent.status)}</span>
                    </div>
                    <p>{agent.role_description || t('employees.noRole', 'No role description yet')}</p>
                    <div className="employee-chip-row">
                      <span>{isOwnedAgent(agent) ? t('employees.chips.mine', 'Owned by me') : t('employees.chips.shared', 'Company shared')}</span>
                      {agent.status === 'running' && agent.last_active_at && <span>{t('employees.chips.recommended', 'Recommended')}</span>}
                      {isLocalAgentRuntime(agent) && <span>{t('employees.chips.local', 'Local runtime')}</span>}
                    </div>
                  </div>
                </div>
              </div>
              <div className="employee-card-actions">
                {isOperatorOnlyAgent(agent) ? (
                  <Link to={`/agents/${agent.id}?manage=true#chat`}>
                    <IconUserCircle size={15} stroke={1.7} />
                    {t('employees.actions.inspect', 'Inspect')}
                    <IconArrowRight size={13} stroke={1.7} />
                  </Link>
                ) : (
                  <>
                    <Link to={`/agents/${agent.id}#chat`}>
                      <IconMessageCircle size={15} stroke={1.7} />
                      {t('employees.actions.chat', 'Chat')}
                    </Link>
                    <Link to={`/agents/${agent.id}#knowledge`}>
                      <IconBrain size={15} stroke={1.7} />
                      {t('employees.actions.memory', 'Memory')}
                    </Link>
                    <Link to={`/agents/${agent.id}#workflows`}>
                      <IconRefresh size={15} stroke={1.7} />
                      {t('employees.actions.workflows', 'Workflows')}
                    </Link>
                    <Link to={`/agents/${agent.id}#a2a`}>
                      <IconRoute size={15} stroke={1.7} />
                      {t('employees.actions.team', 'Team')}
                    </Link>
                    {isLocalAgentRuntime(agent) && (
                      <Link to={`/agents/${agent.id}#workspace`}>
                        <IconDeviceDesktop size={15} stroke={1.7} />
                        {t('employees.actions.local', 'Local')}
                      </Link>
                    )}
                    <Link to={`/agents/${agent.id}`}>
                      <IconUserCircle size={15} stroke={1.7} />
                      {t('employees.actions.detail', 'Detail')}
                      <IconArrowRight size={13} stroke={1.7} />
                    </Link>
                  </>
                )}
              </div>
            </article>
          ))}
        </div>
      </section>
    </div>
  );
}
