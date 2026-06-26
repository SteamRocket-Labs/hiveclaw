import { type ReactNode, useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  IconBrain,
  IconCheckbox,
  IconDeviceDesktop,
  IconFileText,
  IconRefresh,
  IconRoute,
  IconShieldCheck,
  IconUsers,
} from '@tabler/icons-react';
import { agentApi } from '../api/domains/agents';
import { enterpriseApi } from '../api/domains/enterprise';
import { fileApi } from '../api/domains/files';
import { knowledgeApi } from '../api/domains/knowledge';
import { planApi, type PlanRequest } from '../api/domains/plans';
import { triggerApi } from '../api/domains/triggers';
import { autonomyApi } from '../api/domains/autonomy';
import { listWorkflowDefinitions, type WorkflowDefinitionRecord } from '../api/domains/workflows';
import type { Agent } from '../types';
import { useAuthStore } from '../stores';
import {
  buildWakePolicyPayload,
  StaleWorkflowRefError,
  WakeScheduleError,
  type WakeFormState,
  workflowDefinitionFromKey,
  workflowDefinitionOptionKey,
} from './agent-detail/wakePolicyForm';

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

type AutomationSection = 'current' | 'paused';

interface AutomationHubRow {
  id: string;
  agentId: string;
  agentName: string;
  triggerId: string;
  name: string;
  scheduleText: string;
  status: string;
  statusText: string;
  section: AutomationSection;
  href: string;
  updatedAt: string | null;
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
    subtitle: 'Session-local team work, subagent definitions, peer relationships, and local-runtime agents stay visible without mixing their ownership boundaries.',
    icon: <IconRoute size={20} stroke={1.7} />,
  },
};

interface WorkspaceFeatureHubProps {
  kind: HubKind;
  initialAutomationCreateOpen?: boolean;
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

function createDefaultWakeForm(): WakeFormState {
  return {
    mode: 'scheduled_job',
    name: '',
    reason: '',
    scheduleType: 'cron',
    schedulePreset: 'daily',
    dailyTime: '09:00',
    weeklyDay: '1',
    weeklyTime: '09:00',
    cronExpr: '0 9 * * *',
    intervalMinutes: 60,
    onceAt: '',
    eventType: 'on_message',
    maxFires: 1,
    expiresAt: '',
    workflowDefinitionKey: '',
    workflowArgsText: '{}',
  };
}

function triggerScheduleText(trigger: any): string {
  if (typeof trigger?.display_schedule === 'string' && trigger.display_schedule.trim()) {
    return trigger.display_schedule;
  }
  if (trigger?.type === 'cron' && trigger?.config?.expr) {
    const parts = String(trigger.config.expr).trim().split(/\s+/);
    if (parts.length >= 5) {
      const [minute, hour, , , dayOfWeek] = parts;
      const timeText = `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;
      const weekdays: Record<string, string> = {
        '0': 'Sunday',
        '1': 'Monday',
        '2': 'Tuesday',
        '3': 'Wednesday',
        '4': 'Thursday',
        '5': 'Friday',
        '6': 'Saturday',
        '7': 'Sunday',
      };
      if (hour === '*' && minute === '0') return 'Every hour';
      if (dayOfWeek === '*' && hour !== '*' && minute !== '*') return `Every day at ${timeText}`;
      if (weekdays[dayOfWeek] && hour !== '*' && minute !== '*') return `${weekdays[dayOfWeek]} at ${timeText}`;
    }
    return `Cron: ${trigger.config.expr}`;
  }
  if (trigger?.type === 'interval' && trigger?.config?.minutes) {
    const minutes = Number(trigger.config.minutes);
    if (Number.isFinite(minutes) && minutes > 0) return minutes >= 60 ? `Every ${minutes / 60}h` : `Every ${minutes} min`;
  }
  if (trigger?.type === 'once' && trigger?.config?.at) {
    return `Once at ${trigger.config.at}`;
  }
  return String(trigger?.type || 'automation');
}

function runtimeTaskTriggerIds(task: any): Set<string> {
  const diagnostics = task?.diagnostics && typeof task.diagnostics === 'object' ? task.diagnostics : {};
  const metadata = task?.metadata && typeof task.metadata === 'object' ? task.metadata : {};
  return new Set(
    [
      task?.trigger_id,
      task?.triggerId,
      diagnostics.trigger_id,
      diagnostics.trigger_ids,
      metadata.trigger_id,
      metadata.trigger_ids,
    ]
      .flat()
      .filter((value) => value !== undefined && value !== null)
      .map((value) => String(value)),
  );
}

function latestAttemptForTrigger(trigger: any, attempts: any[]): any | null {
  const triggerId = String(trigger?.id || '');
  if (!triggerId) return null;
  const matching = attempts.filter((attempt) => runtimeTaskTriggerIds(attempt).has(triggerId));
  if (matching.length === 0) return trigger?.last_attempt || null;
  return [...matching].sort((a, b) => {
    const left = Date.parse(a?.created_at || a?.started_at || '') || 0;
    const right = Date.parse(b?.created_at || b?.started_at || '') || 0;
    return right - left;
  })[0];
}

function automationStatus(trigger: any, attempts: any[]): Pick<AutomationHubRow, 'status' | 'statusText' | 'section'> {
  const attentionState = String(trigger?.attention_state || '').toLowerCase();
  if (trigger?.is_enabled === false || attentionState === 'paused') {
    return { status: 'paused', statusText: 'paused', section: 'paused' };
  }

  const latestAttempt = latestAttemptForTrigger(trigger, attempts);
  const attemptStatus = String(latestAttempt?.status || '').toLowerCase();
  if (['pending', 'queued', 'running', 'in_progress', 'started'].includes(attemptStatus)) {
    return { status: 'running', statusText: 'running', section: 'current' };
  }
  if (['failed', 'error', 'needs_reconciliation', 'skipped'].includes(attemptStatus) || ['failed_recently', 'missing_model'].includes(attentionState)) {
    return { status: 'failed', statusText: attentionState === 'missing_model' ? 'missing model' : 'failed', section: 'current' };
  }
  if (['completed', 'success', 'succeeded'].includes(attemptStatus)) {
    return { status: 'completed', statusText: 'completed', section: 'current' };
  }
  return { status: attentionState || 'active', statusText: attentionState || 'active', section: 'current' };
}

async function collectAutomationRows(agents: Agent[]): Promise<AutomationHubRow[]> {
  const rows = await Promise.all(
    agents.slice(0, 30).map(async (agent) => {
      try {
        const [triggers, attempts] = await Promise.all([
          triggerApi.list(agent.id),
          autonomyApi.listRuntimeTasks(agent.id, { taskType: 'trigger', limit: 100, diagnostics: true }).catch(() => []),
        ]);
        return triggers.map((trigger: any) => {
          const status = automationStatus(trigger, attempts);
          const name = String(trigger.display_title || trigger.name || trigger.reason || 'Automation task');
          return {
            id: `${agent.id}:${trigger.id}`,
            agentId: agent.id,
            agentName: agent.name,
            triggerId: String(trigger.id),
            name,
            scheduleText: triggerScheduleText(trigger),
            updatedAt: trigger.last_fired_at || trigger.created_at || null,
            href: `/agents/${agent.id}#aware`,
            ...status,
          };
        });
      } catch {
        return [] as AutomationHubRow[];
      }
    }),
  );
  return sortByDateDesc(rows.flat()).slice(0, 80);
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

function apiErrorMessage(error: unknown, fallback: string): string {
  const response = (error as { response?: { data?: unknown } } | null)?.response?.data;
  if (response && typeof response === 'object' && 'detail' in response) {
    const detail = (response as { detail?: unknown }).detail;
    if (typeof detail === 'string') return detail;
    if (detail && typeof detail === 'object' && 'summary' in detail) {
      const summary = (detail as { summary?: unknown }).summary;
      if (typeof summary === 'string') return summary;
    }
  }
  if (error instanceof Error && error.message) return error.message;
  return fallback;
}

function EmptyState({ children }: { children: ReactNode }) {
  return <div className="workbench-empty compact">{children}</div>;
}

export default function WorkspaceFeatureHub({ kind, initialAutomationCreateOpen = false }: WorkspaceFeatureHubProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const currentUser = useAuthStore((state) => state.user);
  const copy = hubCopy[kind];
  const [showAutomationCreate, setShowAutomationCreate] = useState(initialAutomationCreateOpen);
  const [wakeForm, setWakeForm] = useState<WakeFormState>(() => createDefaultWakeForm());
  const [selectedAutomationAgentId, setSelectedAutomationAgentId] = useState('');
  const [automationCreateError, setAutomationCreateError] = useState('');
  const { data: agents = [], isLoading: agentsLoading } = useQuery({
    queryKey: ['agents'],
    queryFn: () => agentApi.list(),
  });
  const idsKey = agentIdsKey(agents);
  const ownedAutomationAgents = currentUser?.id
    ? agents.filter((agent: Agent) => String(agent.creator_id || '') === String(currentUser.id))
    : [];
  const automationIdsKey = agentIdsKey(ownedAutomationAgents);
  const selectedAutomationAgent = selectedAutomationAgentId && ownedAutomationAgents.some((agent) => agent.id === selectedAutomationAgentId)
    ? selectedAutomationAgentId
    : ownedAutomationAgents[0]?.id || '';
  const { data: workflowDefinitions = [], isLoading: workflowsLoading } = useQuery({
    queryKey: ['workflow-definitions', selectedAutomationAgent],
    queryFn: () => listWorkflowDefinitions(selectedAutomationAgent || undefined),
    enabled: kind === 'automations' && showAutomationCreate && !!selectedAutomationAgent,
  });
  const { data: automationRows = [], isLoading: automationRowsLoading } = useQuery({
    queryKey: ['feature-hub-automation-rows', automationIdsKey],
    queryFn: () => collectAutomationRows(ownedAutomationAgents),
    enabled: kind === 'automations' && !!currentUser?.id && ownedAutomationAgents.length > 0,
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
  const currentAutomationRows = automationRows.filter((row) => row.section === 'current');
  const pausedAutomationRows = automationRows.filter((row) => row.section === 'paused');

  const createAutomationTask = async () => {
    const targetAgentId = selectedAutomationAgent || ownedAutomationAgents[0]?.id;
    if (!targetAgentId) {
      setAutomationCreateError(t('featureHub.automationNoAgentSelected', 'Select an agent before creating a task.'));
      return;
    }
    const selectedWorkflow = workflowDefinitionFromKey(wakeForm.workflowDefinitionKey, workflowDefinitions);
    let payload: Record<string, unknown>;
    try {
      payload = buildWakePolicyPayload(wakeForm, selectedWorkflow);
    } catch (error) {
      setAutomationCreateError(
        error instanceof StaleWorkflowRefError
          ? t('agent.aware.workflowRefStale', 'The selected workflow template is no longer available — pick another.')
          : error instanceof WakeScheduleError
            ? t('agent.aware.scheduleInvalid', 'Select a valid schedule.')
            : t('agent.aware.workflowArgsInvalid', 'Workflow args must be valid JSON.'),
      );
      return;
    }

    try {
      await triggerApi.create(targetAgentId, payload);
      setAutomationCreateError('');
      setWakeForm(createDefaultWakeForm());
      setShowAutomationCreate(false);
      await queryClient.invalidateQueries({ queryKey: ['feature-hub-automation-rows'] });
    } catch (error) {
      setAutomationCreateError(apiErrorMessage(error, t('featureHub.automationCreateFailed', 'Could not create this automation task.')));
    }
  };

  const renderAutomationRows = (rows: AutomationHubRow[], emptyText: string) => (
    <div className="automation-task-list">
      {rows.length === 0 && <EmptyState>{emptyText}</EmptyState>}
      {rows.map((row) => (
        <Link key={row.id} to={row.href} className="automation-task-row">
          <span className={`automation-task-dot ${row.status}`} aria-hidden="true" />
          <span className="automation-task-main">
            <strong>{row.name}</strong>
            <small>{row.agentName}</small>
          </span>
          <span className="automation-task-schedule">{row.scheduleText}</span>
          <span className={`employee-status ${row.status}`}>{row.statusText}</span>
        </Link>
      ))}
    </div>
  );

  const renderAutomationCreateDialog = () => {
    if (!showAutomationCreate) return null;
    const activeWorkflowDefinitions = (workflowDefinitions as WorkflowDefinitionRecord[]).filter((record) => record.status === 'active');
    const schedulePreset = wakeForm.schedulePreset || 'daily';
    const timeOptions = Array.from({ length: 24 }, (_, hour) => `${String(hour).padStart(2, '0')}:00`);
    const weekdayOptions = [
      ['1', t('agent.aware.weekdayMonday', 'Monday')],
      ['2', t('agent.aware.weekdayTuesday', 'Tuesday')],
      ['3', t('agent.aware.weekdayWednesday', 'Wednesday')],
      ['4', t('agent.aware.weekdayThursday', 'Thursday')],
      ['5', t('agent.aware.weekdayFriday', 'Friday')],
      ['6', t('agent.aware.weekdaySaturday', 'Saturday')],
      ['0', t('agent.aware.weekdaySunday', 'Sunday')],
    ];

    return (
      <div className="automation-create-backdrop" role="presentation">
        <div
          className="automation-create-dialog"
          role="dialog"
          aria-modal="true"
          aria-label={t('featureHub.manualCreateTask', 'Manual create task')}
        >
          <div className="automation-create-header">
            <input
              className="automation-title-input"
              value={wakeForm.name}
              onChange={(event) => setWakeForm({ ...wakeForm, name: event.target.value })}
              placeholder={t('agent.aware.automationTitle', 'Automation title')}
            />
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => setShowAutomationCreate(false)}
              aria-label={t('common.close', 'Close')}
            >
              x
            </button>
          </div>
          <textarea
            className="automation-prompt-input"
            value={wakeForm.reason}
            onChange={(event) => setWakeForm({ ...wakeForm, reason: event.target.value })}
            placeholder={t('agent.aware.promptPlaceholder', 'Add prompt, for example: check production logs and summarize anomalies')}
            rows={8}
          />
          {wakeForm.workflowDefinitionKey && (
            <textarea
              className="form-input"
              data-testid="wake-workflow-ref-args"
              value={wakeForm.workflowArgsText}
              onChange={(event) => setWakeForm({ ...wakeForm, workflowArgsText: event.target.value })}
              rows={2}
              placeholder={t('agent.aware.workflowArgs', 'Workflow args JSON')}
            />
          )}
          <div className="automation-create-controls">
            <label className="automation-create-field">
              <span>{t('agent.aware.selectAgent', 'Select agent')}</span>
              <select
                className="form-input"
                value={selectedAutomationAgent}
                onChange={(event) => setSelectedAutomationAgentId(event.target.value)}
              >
                {ownedAutomationAgents.map((agent: Agent) => (
                  <option key={agent.id} value={agent.id}>
                    {agent.name || agent.id}
                  </option>
                ))}
              </select>
            </label>
            <select
              className="form-input"
              data-testid="wake-workflow-ref-select"
              value={wakeForm.workflowDefinitionKey}
              onChange={(event) => setWakeForm({ ...wakeForm, workflowDefinitionKey: event.target.value })}
              disabled={workflowsLoading}
            >
              <option value="">{t('agent.aware.noWorkflowRef', 'No workflow')}</option>
              {activeWorkflowDefinitions.map((record) => (
                <option key={record.id} value={workflowDefinitionOptionKey(record)}>
                  {record.name} v{record.definition_version}
                </option>
              ))}
            </select>
            <select
              className="form-input"
              value={schedulePreset}
              onChange={(event) =>
                setWakeForm({
                  ...wakeForm,
                  scheduleType: 'cron',
                  schedulePreset: event.target.value as WakeFormState['schedulePreset'],
                })
              }
            >
              <option value="hourly">{t('agent.aware.everyHour', 'Every hour')}</option>
              <option value="daily">{t('agent.aware.everyDay', 'Every day')}</option>
              <option value="weekly">{t('agent.aware.everyWeek', 'Every week')}</option>
              <option value="custom">{t('agent.aware.customSchedule', 'Custom')}</option>
            </select>
            {schedulePreset === 'daily' && (
              <select
                className="form-input compact"
                value={wakeForm.dailyTime || '09:00'}
                onChange={(event) => setWakeForm({ ...wakeForm, dailyTime: event.target.value })}
              >
                {timeOptions.map((time) => (
                  <option key={time} value={time}>
                    {time}
                  </option>
                ))}
              </select>
            )}
            {schedulePreset === 'weekly' && (
              <>
                <select
                  className="form-input compact"
                  value={wakeForm.weeklyDay || '1'}
                  onChange={(event) => setWakeForm({ ...wakeForm, weeklyDay: event.target.value })}
                >
                  {weekdayOptions.map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
                <select
                  className="form-input compact"
                  value={wakeForm.weeklyTime || '09:00'}
                  onChange={(event) => setWakeForm({ ...wakeForm, weeklyTime: event.target.value })}
                >
                  {timeOptions.map((time) => (
                    <option key={time} value={time}>
                      {time}
                    </option>
                  ))}
                </select>
              </>
            )}
            {schedulePreset === 'custom' && (
              <input
                className="form-input"
                value={wakeForm.cronExpr}
                onChange={(event) => setWakeForm({ ...wakeForm, cronExpr: event.target.value })}
                placeholder="0 9 * * *"
              />
            )}
            <div className="automation-create-actions">
              {automationCreateError && <span className="form-error">{automationCreateError}</span>}
              <button type="button" className="btn btn-ghost" onClick={() => setShowAutomationCreate(false)}>
                {t('common.cancel', 'Cancel')}
              </button>
              <button type="button" className="btn btn-primary" onClick={createAutomationTask}>
                {t('common.create', 'Create')}
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  };

  if (kind === 'automations') {
    return (
      <div className="workbench-page automation-hub-page">
        <div className="automation-hub-header">
          <div>
            <span className="workbench-eyebrow">{t('featureHub.automations.eyebrow', 'User automation tasks')}</span>
            <h1 className="page-title">{t('featureHub.automations.title', 'Automations')}</h1>
          </div>
          <button type="button" className="btn btn-primary" onClick={() => setShowAutomationCreate(true)}>
            {t('featureHub.manualCreateTask', 'Manual create task')}
          </button>
        </div>
        {renderAutomationCreateDialog()}
        {automationRowsLoading && <div className="workbench-empty">{t('common.loading', 'Loading...')}</div>}
        {!automationRowsLoading && (
          <div className="automation-hub-sections">
            <section className="automation-section">
              <h2>{t('featureHub.automationCurrent', 'Current')}</h2>
              {renderAutomationRows(currentAutomationRows, t('featureHub.automationNoCurrent', 'No current automation tasks.'))}
            </section>
            <section className="automation-section">
              <h2>{t('featureHub.automationPaused', 'Paused')}</h2>
              {renderAutomationRows(pausedAutomationRows, t('featureHub.automationNoPaused', 'No paused automation tasks.'))}
            </section>
          </div>
        )}
      </div>
    );
  }

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
                    <small>{t('featureHub.localAgentChannelDesc', 'Local-runtime agents with ordinary Agent permissions, presence, channel chat, and workspace transfer')}</small>
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
