import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  IconBrain,
  IconCheckbox,
  IconDeviceDesktop,
  IconFileText,
  IconRefresh,
  IconRoute,
  IconShieldCheck,
  IconSparkles,
  IconUsers,
} from '@tabler/icons-react';
import { agentApi } from '../api/domains/agents';
import { enterpriseApi } from '../api/domains/enterprise';
import { fileApi } from '../api/domains/files';
import { knowledgeApi } from '../api/domains/knowledge';
import { planApi, type PlanRequest } from '../api/domains/plans';
import { listWorkflowDefinitions, listWorkflowRuns, type WorkflowRunSummary } from '../api/domains/workflows';
import type { Agent } from '../types';

type HubKind = 'plans' | 'automations' | 'memory' | 'documents' | 'approvals' | 'team';

interface HubCopy {
  title: string;
  eyebrow: string;
  subtitle: string;
  icon: ReactNode;
}

interface PlanHubRow {
  agentId: string;
  agentName: string;
  id: string;
  title: string;
  status: string;
  updatedAt: string | null;
  href: string;
}

interface ApprovalHubRow {
  id: string;
  actionType: string;
  agentName: string;
  status: string;
  createdAt: string | null;
}

interface MemoryHubRow {
  agentId: string;
  agentName: string;
  active: number;
  stale: number;
  pendingSoulCandidates: number;
  skillCandidates: number;
  href: string;
}

interface DocumentHubRow {
  agentId: string;
  agentName: string;
  name: string;
  path: string;
  type: string;
  size: number;
  href: string;
}

interface WorkflowRunHubRow {
  agentId: string;
  agentName: string;
  runId: string;
  name: string;
  status: string;
  stepsDone: number;
  stepsTotal: number;
  href: string;
}

const hubCopy: Record<HubKind, HubCopy> = {
  plans: {
    title: 'Plan Review',
    eyebrow: 'Confirmation boundary',
    subtitle: 'Plan Mode stays session-native: review pending plans in the employee conversation where the evidence and timeline live.',
    icon: <IconCheckbox size={20} stroke={1.7} />,
  },
  automations: {
    title: 'Automations',
    eyebrow: 'Workflow and trigger assets',
    subtitle: 'Registered workflows, trigger-backed runs, and skill-grown automation assets remain governed through the existing workflow engine.',
    icon: <IconRefresh size={20} stroke={1.7} />,
  },
  memory: {
    title: 'Memory & Knowledge',
    eyebrow: 'Learning vault',
    subtitle: 'Memory is agent-authored and platform-governed: T0 session truth, T2/T3 distillation, soul, and skill evidence stay under each employee.',
    icon: <IconBrain size={20} stroke={1.7} />,
  },
  documents: {
    title: 'Documents & Research',
    eyebrow: 'Workspace evidence',
    subtitle: 'Files, Office documents, workspace artifacts, and research exports are surfaced through the employee workspace and Office runtime.',
    icon: <IconFileText size={20} stroke={1.7} />,
  },
  approvals: {
    title: 'Approvals',
    eyebrow: 'Governed action queue',
    subtitle: 'Sensitive, external-visible, or high-risk actions stay behind confirmation, approval, and audit surfaces.',
    icon: <IconShieldCheck size={20} stroke={1.7} />,
  },
  team: {
    title: 'A2A / Team',
    eyebrow: 'Delegation and collaboration',
    subtitle: 'Session-local team work, subagent definitions, peer relationships, and user-scoped local runtime channels stay visible without mixing their ownership boundaries.',
    icon: <IconRoute size={20} stroke={1.7} />,
  },
};

interface WorkspaceFeatureHubProps {
  kind: HubKind;
}

function firstAgent(agents: Agent[]) {
  return agents[0];
}

function agentIdsKey(agents: Agent[]): string {
  return agents.map((agent) => agent.id).join(',');
}

function sortByDateDesc<T extends { updatedAt?: string | null; createdAt?: string | null }>(rows: T[]): T[] {
  return [...rows].sort((a, b) => {
    const left = Date.parse(a.updatedAt || a.createdAt || '') || 0;
    const right = Date.parse(b.updatedAt || b.createdAt || '') || 0;
    return right - left;
  });
}

async function collectPlanRows(agents: Agent[]): Promise<PlanHubRow[]> {
  const rows = await Promise.all(
    agents.slice(0, 12).map(async (agent) => {
      try {
        const plans = await planApi.list(agent.id, 20);
        return plans.map((plan: PlanRequest) => ({
          agentId: agent.id,
          agentName: agent.name,
          id: plan.id,
          title: plan.plan_json?.title || plan.original_request,
          status: plan.status,
          updatedAt: plan.updated_at || plan.created_at,
          href: `/agents/${agent.id}#chat`,
        }));
      } catch {
        return [] as PlanHubRow[];
      }
    }),
  );
  const relevant = rows.flat().filter((plan) =>
    ['awaiting_confirmation', 'planning', 'planning_failed', 'confirmed'].includes(plan.status),
  );
  return sortByDateDesc(relevant).slice(0, 12);
}

async function collectMemoryRows(agents: Agent[]): Promise<MemoryHubRow[]> {
  const rows = await Promise.all(
    agents.slice(0, 12).map(async (agent) => {
      try {
        const overview = await knowledgeApi.overview(agent.id);
        return {
          agentId: agent.id,
          agentName: agent.name,
          active: overview.memory.active,
          stale: overview.memory.stale,
          pendingSoulCandidates: overview.identity.pendingSoulCandidates,
          skillCandidates: overview.linkedCapabilities.skillCandidates,
          href: `/agents/${agent.id}#knowledge`,
        };
      } catch {
        return null;
      }
    }),
  );
  return rows.filter((row): row is MemoryHubRow => row !== null);
}

async function collectDocumentRows(agents: Agent[]): Promise<DocumentHubRow[]> {
  const rows = await Promise.all(
    agents.slice(0, 12).map(async (agent) => {
      try {
        const files = await fileApi.list(agent.id);
        return files
          .filter((file) => !String(file.name || '').startsWith('.'))
          .slice(0, 4)
          .map((file) => ({
            agentId: agent.id,
            agentName: agent.name,
            name: file.name,
            path: file.path,
            type: file.type,
            size: typeof file.size === 'number' ? file.size : 0,
            href: `/agents/${agent.id}#workspace`,
          }));
      } catch {
        return [] as DocumentHubRow[];
      }
    }),
  );
  return rows.flat().slice(0, 16);
}

async function collectWorkflowRunRows(agents: Agent[]): Promise<WorkflowRunHubRow[]> {
  const rows = await Promise.all(
    agents.slice(0, 12).map(async (agent) => {
      try {
        const runs = await listWorkflowRuns(agent.id, 8);
        return runs.map((run: WorkflowRunSummary) => ({
          agentId: agent.id,
          agentName: agent.name,
          runId: run.run_id,
          name: run.name,
          status: run.status,
          stepsDone: run.steps_done,
          stepsTotal: run.steps_total,
          href: `/agents/${agent.id}#workflows`,
        }));
      } catch {
        return [] as WorkflowRunHubRow[];
      }
    }),
  );
  return rows.flat().slice(0, 12);
}

async function collectApprovalRows(): Promise<ApprovalHubRow[]> {
  const approvals = await enterpriseApi.listApprovals();
  return (approvals as Array<Record<string, unknown>>).slice(0, 16).map((approval) => ({
    id: String(approval.id || ''),
    actionType: String(approval.action_type || approval.action || 'approval'),
    agentName: String(approval.agent_name || approval.agent_id || 'Agent'),
    status: String(approval.status || 'pending'),
    createdAt: typeof approval.created_at === 'string' ? approval.created_at : null,
  }));
}

function formatDate(value: string | null | undefined): string {
  if (!value) return '';
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return '';
  return new Date(timestamp).toLocaleString();
}

function formatFileSize(size: number): string {
  if (!Number.isFinite(size) || size <= 0) return '0 B';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function EmptyState({ children }: { children: ReactNode }) {
  return <div className="workbench-empty compact">{children}</div>;
}

export default function WorkspaceFeatureHub({ kind }: WorkspaceFeatureHubProps) {
  const { t } = useTranslation();
  const copy = hubCopy[kind];
  const { data: agents = [], isLoading: agentsLoading } = useQuery({
    queryKey: ['agents'],
    queryFn: () => agentApi.list(),
  });
  const idsKey = agentIdsKey(agents);
  const { data: workflowDefinitions = [], isLoading: workflowsLoading } = useQuery({
    queryKey: ['workflow-definitions'],
    queryFn: () => listWorkflowDefinitions(),
    enabled: kind === 'automations',
  });
  const { data: workflowRunRows = [], isLoading: workflowRunsLoading } = useQuery({
    queryKey: ['feature-hub-workflow-runs', idsKey],
    queryFn: () => collectWorkflowRunRows(agents),
    enabled: kind === 'automations' && agents.length > 0,
  });
  const { data: planRows = [], isLoading: plansLoading } = useQuery({
    queryKey: ['feature-hub-plans', idsKey],
    queryFn: () => collectPlanRows(agents),
    enabled: kind === 'plans' && agents.length > 0,
  });
  const { data: memoryRows = [], isLoading: memoryLoading } = useQuery({
    queryKey: ['feature-hub-memory', idsKey],
    queryFn: () => collectMemoryRows(agents),
    enabled: kind === 'memory' && agents.length > 0,
  });
  const { data: documentRows = [], isLoading: documentsLoading } = useQuery({
    queryKey: ['feature-hub-documents', idsKey],
    queryFn: () => collectDocumentRows(agents),
    enabled: kind === 'documents' && agents.length > 0,
  });
  const { data: approvalRows = [], isLoading: approvalsLoading } = useQuery({
    queryKey: ['feature-hub-approvals'],
    queryFn: () => collectApprovalRows(),
    enabled: kind === 'approvals',
  });
  const primaryAgent = firstAgent(agents);

  return (
    <div className="workbench-page">
      <div className="workbench-hero">
        <div>
          <span className="workbench-eyebrow">{t(`featureHub.${kind}.eyebrow`, copy.eyebrow)}</span>
          <h1 className="page-title">{t(`featureHub.${kind}.title`, copy.title)}</h1>
          <p className="page-subtitle">{t(`featureHub.${kind}.subtitle`, copy.subtitle)}</p>
        </div>
        <span className="workbench-hero-icon">{copy.icon}</span>
      </div>

      <div className="workbench-grid two">
        <section className="workbench-panel">
          <div className="workbench-panel-header">
            <div>
              <h2>{t('featureHub.employeeSurfaces', 'Employee surfaces')}</h2>
              <p>{t('featureHub.employeeSurfacesDesc', 'Every shortcut opens a real existing employee surface, preserving session and governance ownership.')}</p>
            </div>
          </div>
          {agentsLoading && <div className="workbench-empty">{t('common.loading', 'Loading...')}</div>}
          {!agentsLoading && agents.length === 0 && <div className="workbench-empty">{t('featureHub.noAgents', 'Create an employee to use this area.')}</div>}
          <div className="feature-link-list">
            {agents.slice(0, 8).map((agent: Agent) => {
              const target =
                kind === 'memory' ? `/agents/${agent.id}#knowledge`
                  : kind === 'documents' ? `/agents/${agent.id}#workspace`
                    : kind === 'approvals' ? `/agents/${agent.id}#approvals`
                      : kind === 'automations' ? `/agents/${agent.id}#workflows`
                        : `/agents/${agent.id}#chat`;
              return (
                <Link key={agent.id} to={target} className="feature-link-row">
                  <span className="employee-avatar small">{(Array.from(agent.name || 'A')[0] as string || 'A').toUpperCase()}</span>
                  <span>
                    <strong>{agent.name}</strong>
                    <small>{agent.role_description || t('employees.noRole', 'No role description yet')}</small>
                  </span>
                </Link>
              );
            })}
          </div>
        </section>

        <section className="workbench-panel">
          <div className="workbench-panel-header">
            <div>
              <h2>{t('featureHub.controlLinks', 'Control plane links')}</h2>
              <p>{t('featureHub.controlLinksDesc', 'Company-level settings remain in the control plane; the employee still owns runtime execution.')}</p>
            </div>
          </div>
          <div className="feature-link-list">
            {kind === 'automations' && (
              <>
                <Link to="/enterprise/skills" className="feature-link-row">
                  <IconSparkles size={18} stroke={1.7} />
                  <span>
                    <strong>{t('featureHub.skillRegistry', 'Skill registry')}</strong>
                    <small>{t('featureHub.skillRegistryDesc', 'Skill capsules and evolution candidates')}</small>
                  </span>
                </Link>
                <Link to={primaryAgent ? `/agents/${primaryAgent.id}#workflows` : '/agents'} className="feature-link-row">
                  <IconRefresh size={18} stroke={1.7} />
                  <span>
                    <strong>{t('featureHub.workflowWorkbench', 'Workflow workbench')}</strong>
                    <small>{t('featureHub.workflowWorkbenchDesc', 'Preview, start, inspect, and promote workflow runs')}</small>
                  </span>
                </Link>
              </>
            )}
            {kind === 'memory' && (
              <Link to="/enterprise/memory" className="feature-link-row">
                <IconShieldCheck size={18} stroke={1.7} />
                <span>
                  <strong>{t('featureHub.memoryGovernance', 'Memory governance')}</strong>
                  <small>{t('featureHub.memoryGovernanceDesc', 'Retention, hygiene, and governed write policy')}</small>
                </span>
              </Link>
            )}
            {kind === 'documents' && (
              <Link to={primaryAgent ? `/agents/${primaryAgent.id}#office` : '/agents'} className="feature-link-row">
                <IconFileText size={18} stroke={1.7} />
                <span>
                  <strong>{t('featureHub.officeRuntime', 'Office runtime')}</strong>
                  <small>{t('featureHub.officeRuntimeDesc', 'Browser document editing backed by agent workspace files')}</small>
                </span>
              </Link>
            )}
            {kind === 'plans' && (
              <Link to={primaryAgent ? `/agents/${primaryAgent.id}#chat` : '/agents'} className="feature-link-row">
                <IconCheckbox size={18} stroke={1.7} />
                <span>
                  <strong>{t('featureHub.sessionPlans', 'Session plan queue')}</strong>
                  <small>{t('featureHub.sessionPlansDesc', 'Plans are accepted or revised in the active session timeline')}</small>
                </span>
              </Link>
            )}
            {kind === 'approvals' && (
              <Link to="/enterprise/approvals" className="feature-link-row">
                <IconShieldCheck size={18} stroke={1.7} />
                <span>
                  <strong>{t('featureHub.approvalCenter', 'Approval center')}</strong>
                  <small>{t('featureHub.approvalCenterDesc', 'Company approval review, audit, and policy controls')}</small>
                </span>
              </Link>
            )}
            {kind === 'team' && (
              <>
                <Link to="/enterprise/subagents" className="feature-link-row">
                  <IconUsers size={18} stroke={1.7} />
                  <span>
                    <strong>{t('featureHub.companySubagents', 'Company subagent library')}</strong>
                    <small>{t('featureHub.companySubagentsDesc', 'Tenant-level worker definitions shared by employee runtimes')}</small>
                  </span>
                </Link>
                <Link to="/local-agents" className="feature-link-row">
                  <IconDeviceDesktop size={18} stroke={1.7} />
                  <span>
                    <strong>{t('featureHub.localAgentChannel', 'Local Agent Channel')}</strong>
                    <small>{t('featureHub.localAgentChannelDesc', 'User-scoped local runtime presence, channel chat, and workspace transfer')}</small>
                  </span>
                </Link>
              </>
            )}
          </div>
        </section>
      </div>

      {kind === 'plans' && (
        <section className="workbench-panel">
          <div className="workbench-panel-header">
            <div>
              <h2>{t('featureHub.planQueueTitle', 'Cross-agent plan queue')}</h2>
              <p>{t('featureHub.planQueueDesc', 'Pending and recently confirmed plans stay anchored to the owning employee session.')}</p>
            </div>
          </div>
          {plansLoading && <div className="workbench-empty">{t('common.loading', 'Loading...')}</div>}
          {!plansLoading && planRows.length === 0 && <EmptyState>{t('featureHub.noPlans', 'No pending plans across employees.')}</EmptyState>}
          <div className="workbench-aggregate-list">
            {planRows.map((plan) => (
              <Link key={`${plan.agentId}:${plan.id}`} to={plan.href} className="workbench-aggregate-row">
                <span className={`employee-status ${plan.status}`}>{plan.status}</span>
                <span>
                  <strong>{plan.title}</strong>
                  <small>{plan.agentName}{formatDate(plan.updatedAt) ? ` · ${formatDate(plan.updatedAt)}` : ''}</small>
                </span>
              </Link>
            ))}
          </div>
        </section>
      )}

      {kind === 'memory' && (
        <section className="workbench-panel">
          <div className="workbench-panel-header">
            <div>
              <h2>{t('featureHub.memoryHealthTitle', 'Memory health')}</h2>
              <p>{t('featureHub.memoryHealthDesc', 'A read model over each employee knowledge plane; durable writes still happen inside the governed memory path.')}</p>
            </div>
          </div>
          {memoryLoading && <div className="workbench-empty">{t('common.loading', 'Loading...')}</div>}
          {!memoryLoading && memoryRows.length === 0 && <EmptyState>{t('featureHub.noMemoryRows', 'No memory overview available yet.')}</EmptyState>}
          <div className="workbench-data-grid">
            {memoryRows.map((row) => (
              <Link key={row.agentId} to={row.href} className="workbench-data-card">
                <strong>{row.agentName}</strong>
                <div className="workbench-stat-grid">
                  <span><small>{t('featureHub.activeMemories', 'Active memories')}</small><b>{row.active}</b></span>
                  <span><small>{t('featureHub.staleMemories', 'Stale')}</small><b>{row.stale}</b></span>
                  <span><small>{t('featureHub.soulCandidates', 'Soul candidates')}</small><b>{row.pendingSoulCandidates}</b></span>
                  <span><small>{t('featureHub.skillCandidates', 'Skill candidates')}</small><b>{row.skillCandidates}</b></span>
                </div>
              </Link>
            ))}
          </div>
        </section>
      )}

      {kind === 'documents' && (
        <section className="workbench-panel">
          <div className="workbench-panel-header">
            <div>
              <h2>{t('featureHub.documentsTitle', 'Workspace evidence')}</h2>
              <p>{t('featureHub.documentsDesc', 'Recent root workspace files; open the employee workspace for editing and Office runtime actions.')}</p>
            </div>
          </div>
          {documentsLoading && <div className="workbench-empty">{t('common.loading', 'Loading...')}</div>}
          {!documentsLoading && documentRows.length === 0 && <EmptyState>{t('featureHub.noDocuments', 'No workspace files surfaced yet.')}</EmptyState>}
          <div className="workbench-aggregate-list">
            {documentRows.map((file) => (
              <Link key={`${file.agentId}:${file.path}`} to={file.href} className="workbench-aggregate-row">
                <span className="employee-status">{file.type}</span>
                <span>
                  <strong>{file.name}</strong>
                  <small>{file.agentName} · {file.path} · {formatFileSize(file.size)}</small>
                </span>
              </Link>
            ))}
          </div>
        </section>
      )}

      {kind === 'approvals' && (
        <section className="workbench-panel">
          <div className="workbench-panel-header">
            <div>
              <h2>{t('featureHub.approvalQueueTitle', 'Approval queue')}</h2>
              <p>{t('featureHub.approvalQueueDesc', 'Company-level approval state is visible here; resolution remains in the governed approval center.')}</p>
            </div>
          </div>
          {approvalsLoading && <div className="workbench-empty">{t('common.loading', 'Loading...')}</div>}
          {!approvalsLoading && approvalRows.length === 0 && <EmptyState>{t('featureHub.noApprovals', 'No approvals are waiting.')}</EmptyState>}
          <div className="workbench-aggregate-list">
            {approvalRows.map((approval) => (
              <Link key={approval.id} to="/enterprise/approvals" className="workbench-aggregate-row">
                <span className={`employee-status ${approval.status}`}>{approval.status}</span>
                <span>
                  <strong>{approval.actionType}</strong>
                  <small>{approval.agentName}{formatDate(approval.createdAt) ? ` · ${formatDate(approval.createdAt)}` : ''}</small>
                </span>
              </Link>
            ))}
          </div>
        </section>
      )}

      {kind === 'automations' && (
        <>
          <section className="workbench-panel">
            <div className="workbench-panel-header">
              <div>
                <h2>{t('featureHub.registeredWorkflows', 'Registered workflows')}</h2>
                <p>{t('featureHub.registeredWorkflowsDesc', 'Read from the workflow definition API; activation and promotion stay in the workflow runtime.')}</p>
              </div>
            </div>
            {workflowsLoading && <div className="workbench-empty">{t('common.loading', 'Loading...')}</div>}
            {!workflowsLoading && workflowDefinitions.length === 0 && (
              <EmptyState>{t('featureHub.noWorkflows', 'No registered workflows yet.')}</EmptyState>
            )}
            <div className="workflow-definition-grid">
              {workflowDefinitions.map((definition: any) => (
                <article key={definition.id} className="workflow-definition-card">
                  <span className={`employee-status ${definition.status}`}>{definition.status}</span>
                  <h3>{definition.name}</h3>
                  <p>{definition.description || t('featureHub.noDescription', 'No description')}</p>
                  <small>v{definition.definition_version}</small>
                </article>
              ))}
            </div>
          </section>

          <section className="workbench-panel">
            <div className="workbench-panel-header">
              <div>
                <h2>{t('featureHub.workflowRunsTitle', 'Recent workflow runs')}</h2>
                <p>{t('featureHub.workflowRunsDesc', 'Runtime evidence from employee workflow histories; promotion remains inside each employee workbench.')}</p>
              </div>
            </div>
            {workflowRunsLoading && <div className="workbench-empty">{t('common.loading', 'Loading...')}</div>}
            {!workflowRunsLoading && workflowRunRows.length === 0 && <EmptyState>{t('featureHub.noWorkflowRuns', 'No workflow runs surfaced yet.')}</EmptyState>}
            <div className="workbench-aggregate-list">
              {workflowRunRows.map((run) => (
                <Link key={`${run.agentId}:${run.runId}`} to={run.href} className="workbench-aggregate-row">
                  <span className={`employee-status ${run.status}`}>{run.status}</span>
                  <span>
                    <strong>{run.name}</strong>
                    <small>{run.agentName} · {run.stepsDone}/{run.stepsTotal} steps</small>
                  </span>
                </Link>
              ))}
            </div>
          </section>
        </>
      )}

      {kind === 'team' && (
        <section className="workbench-panel">
          <div className="workbench-panel-header">
            <div>
              <h2>{t('featureHub.teamTopologyTitle', 'A2A and team topology')}</h2>
              <p>{t('featureHub.teamTopologyDesc', 'Session-local teams are entered from the conversation; durable org delegation and reusable workers stay attached to each employee and the control plane.')}</p>
            </div>
          </div>
          <div className="workbench-grid three">
            <Link to={primaryAgent ? `/agents/${primaryAgent.id}#chat` : '/agents'} className="workbench-data-card">
              <strong>{t('featureHub.sessionLocalTeam', 'Session-local team')}</strong>
              <p>{t('featureHub.sessionLocalTeamDesc', 'Team windows belong to the active conversation timeline and are reconciled back into the main session.')}</p>
            </Link>
            <Link to={primaryAgent ? `/agents/${primaryAgent.id}#relationships` : '/agents'} className="workbench-data-card">
              <strong>{t('featureHub.orgDelegation', 'Org delegation')}</strong>
              <p>{t('featureHub.orgDelegationDesc', 'Peer relationships and A2A collaboration stay explicit at employee level when ownership crosses boundaries.')}</p>
            </Link>
            <Link to="/local-agents" className="workbench-data-card">
              <strong>{t('featureHub.localAgentChannel', 'Local Agent Channel')}</strong>
              <p>{t('featureHub.localAgentChannelDesc', 'Local runtime is user-scoped channel presence, not an agent detail tab.')}</p>
            </Link>
          </div>
          <div className="workbench-aggregate-list" style={{ marginTop: 16 }}>
            {agents.slice(0, 12).map((agent: Agent) => (
              <div key={agent.id} className="workbench-aggregate-row">
                <span className={`employee-status ${agent.execution_mode || 'standard'}`}>{agent.execution_mode || 'standard'}</span>
                <span>
                  <strong>{agent.name}</strong>
                  <small>{agent.role_description || t('employees.noRole', 'No role description yet')}</small>
                </span>
                <span className="feature-inline-actions">
                  <Link to={`/agents/${agent.id}#relationships`}>{t('featureHub.relationships', 'Relationships')}</Link>
                  <Link to={`/agents/${agent.id}#subagents`}>{t('featureHub.subagents', 'Subagents')}</Link>
                </span>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
