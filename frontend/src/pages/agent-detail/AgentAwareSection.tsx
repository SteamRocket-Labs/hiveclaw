import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import './AgentAwareSection.css';
import { chatApi } from '../../api/domains/chat';
import { triggerApi } from '../../api/domains/triggers';
import { autonomyApi } from '../../api/domains/autonomy';
import { agentApi } from '../../api/domains/agents';
import { listWorkflowDefinitions } from '../../api/domains/workflows';
import { requestAppConfirm } from '../../components/AppDialogs';
import { StructuredToolResultBody } from './StructuredToolResult';
import TeamMemorySummaryCard from './TeamMemorySummaryCard';
import PlanQueueSection from './PlanQueueSection';
import { normalizeToolCallResult } from './toolResultEnvelope';
import {
  buildWakePolicyPayload,
  StaleWorkflowRefError,
  WakeScheduleError,
  type WakeFormState,
  type WakeSchedulePreset,
  workflowDefinitionFromKey,
  workflowDefinitionOptionKey,
} from './wakePolicyForm';

export {
  buildWakePolicyPayload,
  StaleWorkflowRefError,
  WakeScheduleError,
  workflowDefinitionFromKey,
  workflowDefinitionOptionKey,
};
export type { WakeFormState, WakeSchedulePreset } from './wakePolicyForm';

type AgentAwareSectionProps = {
  agentId: string;
  awareTriggers: any[];
  reflectionSessions: any[];
  reflectionMessages: Record<string, any[]>;
  expandedReflection: string | null;
  showAllTriggers: boolean;
  reflectionPage: number;
  onSetExpandedReflection: React.Dispatch<React.SetStateAction<string | null>>;
  onSetReflectionMessages: React.Dispatch<React.SetStateAction<Record<string, any[]>>>;
  onSetShowAllTriggers: React.Dispatch<React.SetStateAction<boolean>>;
  onSetReflectionPage: React.Dispatch<React.SetStateAction<number>>;
  onRefetchTriggers: () => void | Promise<unknown>;
  onLoadReflectionMessages?: (sessionId: string) => Promise<any[] | void>;
  autonomyOverview?: any | null;
  onRefetchAutonomy?: () => void | Promise<unknown>;
  initialShowCreateWake?: boolean;
};

const REFLECTIONS_PAGE_SIZE = 10;
const SECTION_PAGE_SIZE = 5;

export default function AgentAwareSection({
  agentId,
  awareTriggers,
  reflectionSessions,
  reflectionMessages,
  expandedReflection,
  showAllTriggers,
  reflectionPage,
  onSetExpandedReflection,
  onSetReflectionMessages,
  onSetShowAllTriggers,
  onSetReflectionPage,
  onRefetchTriggers,
  onLoadReflectionMessages,
  autonomyOverview,
  onRefetchAutonomy,
  initialShowCreateWake = false,
}: AgentAwareSectionProps) {
  const { t, i18n } = useTranslation();
  const [artifactView, setArtifactView] = React.useState<any | null>(null);
  const [artifactLoading, setArtifactLoading] = React.useState(false);
  const [showCreateWake, setShowCreateWake] = React.useState(initialShowCreateWake);
  const [selectedWakeAgentId, setSelectedWakeAgentId] = React.useState(agentId);
  const [wakeForm, setWakeForm] = React.useState<WakeFormState>({
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
  });
  const [wakeError, setWakeError] = React.useState('');

  const { data: workflowDefinitions = [] } = useQuery({
    queryKey: ['workflow-definitions', agentId],
    queryFn: () => listWorkflowDefinitions(agentId),
    enabled: !!agentId,
  });
  const { data: agentOptions = [] } = useQuery({
    queryKey: ['agents'],
    queryFn: () => agentApi.list(),
    enabled: showCreateWake,
  });

  React.useEffect(() => {
    setSelectedWakeAgentId(agentId);
  }, [agentId]);

  const triggerToHuman = (trigger: any): string => {
    if (trigger.type === 'cron' && trigger.config?.expr) {
      const expression = trigger.config.expr;
      const parts = expression.split(' ');
      if (parts.length >= 5) {
        const [minute, hour, , , dayOfWeek] = parts;
        const timeText = `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;
        if (dayOfWeek === '*' && minute !== '*' && hour !== '*') return t('agent.aware.scheduleEveryDayAt', 'Every day at {{time}}', { time: timeText });
        if (dayOfWeek === '1-5' && minute !== '*' && hour !== '*') return t('agent.aware.scheduleWeekdaysAt', 'Weekdays at {{time}}', { time: timeText });
        if (dayOfWeek === '0' || dayOfWeek === '7') return t('agent.aware.scheduleSundaysAt', 'Sundays at {{time}}', { time: timeText });
        if (hour === '*' && minute === '0') {
          if (dayOfWeek === '1-5') return t('agent.aware.scheduleHourlyWeekdays', 'Every hour on weekdays');
          return t('agent.aware.scheduleHourly', 'Every hour');
        }
        if (hour === '*' && minute !== '*') return t('agent.aware.scheduleHourlyAt', 'Every hour at :{{minute}}', { minute: String(minute).padStart(2, '0') });
      }
      return t('agent.aware.scheduleCron', 'Cron: {{expression}}', { expression });
    }
    if (trigger.type === 'once' && trigger.config?.at) {
      try {
        const time = new Date(trigger.config.at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
        return t('agent.aware.scheduleOnceAt', 'Once at {{time}}', { time });
      } catch {
        return t('agent.aware.scheduleOnceAt', 'Once at {{time}}', { time: String(trigger.config.at) });
      }
    }
    if (trigger.type === 'interval' && trigger.config?.minutes) {
      const minutes = Number(trigger.config.minutes);
      if (minutes >= 60 && minutes % 60 === 0) return t('agent.aware.scheduleEveryHours', 'Every {{count}}h', { count: minutes / 60 });
      return t('agent.aware.scheduleEveryMinutes', 'Every {{count}} min', { count: minutes });
    }
    if (trigger.type === 'poll') return t('agent.aware.schedulePoll', 'Poll: {{url}}', { url: trigger.config?.url?.substring(0, 40) || 'URL' });
    if (trigger.type === 'on_message') {
      const source = trigger.config?.from_agent_name || trigger.config?.from_user_name || t('agent.aware.scheduleUnknownSource', 'unknown');
      return t('agent.aware.scheduleOnMessage', 'On message from {{source}}', { source });
    }
    if (trigger.type === 'webhook') return t('agent.aware.scheduleWebhook', 'Webhook');
    return String(trigger.type || '');
  };

  const standaloneTriggers = awareTriggers;
  const hasStandalone = standaloneTriggers.length > 0;

  const loadReflectionMessages = async (sessionId: string) => {
    if (reflectionMessages[sessionId]) return;
    try {
      const data = onLoadReflectionMessages ? await onLoadReflectionMessages(sessionId) : await chatApi.getSessionMessages(String(agentId), String(sessionId));
      if (data) {
        onSetReflectionMessages((previous) => ({ ...previous, [sessionId]: data }));
      }
    } catch {
      // Ignore reflection fetch failures in the UI shell.
    }
  };

  const refreshAutonomy = async () => {
    await onRefetchTriggers();
    if (onRefetchAutonomy) await onRefetchAutonomy();
  };

  // Trigger/wake state enums come from autonomy_overview as stable snake_case
  // codes; map the known vocabulary to translations. Unknown codes must not be
  // prettified into user-facing prose — normal users see a typed unknown label
  // and the raw code stays behind the title tooltip as progressive disclosure
  // (full operator/audit evidence lives in the activity log).
  const statusLabel = (value?: string | null) => {
    const code = String(value || 'unknown');
    const known: Record<string, string> = {
      active: t('agent.aware.stateActive', 'Active'),
      waiting_approval: t('agent.aware.stateWaitingApproval', 'Waiting for approval'),
      missing_model: t('agent.aware.stateMissingModel', 'Model not configured'),
      backoff_active: t('agent.aware.stateBackoff', 'Cooling down'),
      failed_recently: t('agent.aware.stateFailedRecently', 'Failed recently'),
      max_fires_reached: t('agent.aware.stateMaxFires', 'Run limit reached'),
      needs_reconciliation: t('agent.aware.stateNeedsReconciliation', 'Needs admin review'),
      no_recent_attempt: t('agent.aware.stateNoRecentAttempt', 'No recent run'),
      no_wake_policy: t('agent.aware.stateNoWakePolicy', 'No wake policy'),
      has_wake_policy: t('agent.aware.stateHasWakePolicy', 'Wake policy ready'),
      completed: t('agent.aware.stateCompleted', 'Completed'),
      blocked: t('agent.aware.stateBlocked', 'Blocked'),
      stale: t('agent.aware.stateStale', 'Stale'),
      expired: t('agent.aware.stateExpired', 'Expired'),
      paused: t('agent.aware.statePaused', 'Paused'),
      unknown: t('agent.aware.stateUnknown', 'Unknown'),
    };
    if (known[code]) return known[code];
    return code && code !== 'unknown'
      ? t('agent.aware.stateUnknownState', 'Unknown state')
      : known.unknown;
  };

  // User-facing reason text derives from the stable attention_state code the
  // backend already guarantees; the backend English prose (attention_reason)
  // and any unmapped codes stay out of the normal-user DOM and remain in the
  // payload for operator/audit consumers.
  const attentionReasonLabel = (state?: string | null): string | null => {
    const code = String(state || '');
    const known: Record<string, string> = {
      paused: t('agent.aware.reasonPaused', 'Autonomous wake is paused.'),
      expired: t('agent.aware.reasonExpired', 'This wake policy has expired.'),
      max_fires_reached: t('agent.aware.reasonMaxFires', 'This wake policy reached its run limit.'),
      backoff_active: t('agent.aware.reasonBackoff', 'Cooling down after a recent failure.'),
      missing_model: t('agent.aware.reasonMissingModel', 'No model is configured for this autonomous run.'),
      failed_recently: t('agent.aware.reasonFailedRecently', 'The most recent run failed or was skipped.'),
      needs_reconciliation: t('agent.aware.reasonNeedsReconciliation', 'This run needs admin reconciliation before retry.'),
    };
    return known[code] || null;
  };

  const attemptReasonLabel = (status?: string | null): string | null => {
    const code = String(status || '');
    if (code === 'failed' || code === 'skipped') {
      return t('agent.aware.reasonFailedRecently', 'The most recent run failed or was skipped.');
    }
    if (code === 'needs_reconciliation') {
      return t('agent.aware.reasonNeedsReconciliation', 'This run needs admin reconciliation before retry.');
    }
    return null;
  };

  const findingMessageLabel = (finding: any): string | null => {
    const category = String(finding?.category || '');
    if (!category.startsWith('trigger_')) return null;
    return attentionReasonLabel(category.slice('trigger_'.length));
  };

  // Structured schedule facts (backend `schedule` field) render localized;
  // the English display_schedule string is a legacy/operator fallback.
  const scheduleLabelFromView = (trigger: any): string | null => {
    const schedule = trigger?.schedule as Record<string, unknown> | null | undefined;
    if (schedule && typeof schedule === 'object' && typeof schedule.kind === 'string') {
      const kind = schedule.kind;
      if (kind === 'interval' && schedule.minutes != null) {
        const minutes = Number(schedule.minutes);
        if (Number.isFinite(minutes) && minutes > 0) {
          if (minutes >= 60 && minutes % 60 === 0) {
            return t('agent.aware.scheduleEveryHours', 'Every {{count}}h', { count: minutes / 60 });
          }
          return t('agent.aware.scheduleEveryMinutes', 'Every {{count}} min', { count: minutes });
        }
      }
      if (kind === 'once' && typeof schedule.at === 'string' && schedule.at) {
        return t('agent.aware.scheduleOnceAt', 'Once at {{time}}', { time: schedule.at });
      }
      if (kind === 'cron' && typeof schedule.expr === 'string' && schedule.expr) {
        return t('agent.aware.scheduleCron', 'Cron: {{expression}}', { expression: schedule.expr });
      }
      if (kind === 'on_message') return t('agent.aware.scheduleWaitMessage', 'Wait for message');
      if (kind === 'webhook') return t('agent.aware.scheduleWebhook', 'Webhook');
      if (kind === 'poll') return t('agent.aware.schedulePollLabel', 'Polling');
    }
    const fallback = trigger?.display_schedule;
    return typeof fallback === 'string' && fallback ? fallback : null;
  };

  const kindLabel = (value?: string | null) => {
    const key = String(value || '');
    if (key === 'scheduled_job') return t('agent.aware.kindScheduled', 'Scheduled job');
    if (key === 'event_wait') return t('agent.aware.kindEventWait', 'Waiting event');
    if (key === 'system_maintenance') return t('agent.aware.kindMaintenance', 'System maintenance');
    return key || t('agent.aware.kindWake', 'Wake policy');
  };

  const stateToneClass = (state?: string | null): string => {
    if (['active', 'has_wake_policy', 'completed'].includes(String(state))) return 'agent-aware-tone-success';
    if (['waiting_approval', 'missing_model', 'no_wake_policy', 'backoff_active'].includes(String(state))) return 'agent-aware-tone-warning';
    if (['blocked', 'stale', 'failed_recently', 'expired'].includes(String(state))) return 'agent-aware-tone-error';
    return 'agent-aware-tone-muted';
  };

  const statusBadge = (state?: string | null) => (
    <span className={`agent-aware-status-badge ${stateToneClass(state)}`}>{statusLabel(state)}</span>
  );

  const pauseOrResumeTrigger = async (trigger: any, enabled: boolean) => {
    await triggerApi.update(agentId, trigger.id, { is_enabled: enabled });
    await refreshAutonomy();
  };

  const viewArtifact = async (attempt: any) => {
    const taskId = attempt?.task_id;
    if (!taskId) return;
    setArtifactLoading(true);
    try {
      const payload = await autonomyApi.getRuntimeArtifact(agentId, taskId);
      setArtifactView(payload);
    } finally {
      setArtifactLoading(false);
    }
  };

  const createWakePolicy = async () => {
    const selectedWorkflow = workflowDefinitionFromKey(wakeForm.workflowDefinitionKey, workflowDefinitions);
    let payload: Record<string, unknown>;
    try {
      payload = buildWakePolicyPayload(wakeForm, selectedWorkflow);
      setWakeError('');
    } catch (error) {
      setWakeError(
        error instanceof StaleWorkflowRefError
          ? t('agent.aware.workflowRefStale', 'The selected workflow template is no longer available — pick another.')
          : error instanceof WakeScheduleError
            ? t('agent.aware.scheduleInvalid', 'Select a valid schedule.')
          : t('agent.aware.workflowArgsInvalid', 'Workflow args must be valid JSON.'),
      );
      return;
    }
    const targetAgentId = selectedWakeAgentId || agentId;
    await triggerApi.create(targetAgentId, payload);
    setShowCreateWake(false);
    if (targetAgentId === agentId) {
      await refreshAutonomy();
    }
  };

  const renderCreateWakeForm = () => {
    if (!showCreateWake) return null;
    const activeWorkflowDefinitions = workflowDefinitions.filter((record) => record.status === 'active');
    const agents = Array.isArray(agentOptions) && agentOptions.length > 0
      ? agentOptions
      : [{ id: agentId, name: t('agent.aware.currentAgent', 'Current agent') }];
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
    const schedulePreset = wakeForm.schedulePreset || 'daily';
    return (
      <div role="presentation" className="ui-modal-overlay">
        <div
          role="dialog"
          aria-modal="true"
          aria-label={t('agent.aware.manualCreate', 'Manual create')}
          className="agent-aware-wake-modal"
        >
          <div className="agent-aware-wake-head">
            <input
              className="form-input agent-aware-wake-title-input"
              value={wakeForm.name}
              onChange={(event) => setWakeForm({ ...wakeForm, name: event.target.value })}
              placeholder={t('agent.aware.automationTitle', 'Automation title')}
            />
            <button className="btn btn-ghost" onClick={() => setShowCreateWake(false)} aria-label={t('common.close', 'Close')}>
              x
            </button>
          </div>
          <textarea
            className="form-input agent-aware-wake-prompt-input"
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
          <div className="agent-aware-wake-controls">
            <label className="agent-aware-wake-label">
              <span>{t('agent.aware.selectAgent', 'Select agent')}</span>
              <select
                className="form-input agent-aware-wake-agent-select"
                value={selectedWakeAgentId}
                onChange={(event) => setSelectedWakeAgentId(event.target.value)}
              >
                {agents.map((agent: any) => (
                  <option key={agent.id} value={agent.id}>
                    {agent.name || agent.id}
                  </option>
                ))}
              </select>
            </label>
            <select
              className="form-input agent-aware-wake-ref-select"
              data-testid="wake-workflow-ref-select"
              value={wakeForm.workflowDefinitionKey}
              onChange={(event) => setWakeForm({ ...wakeForm, workflowDefinitionKey: event.target.value })}
            >
              <option value="">{t('agent.aware.noWorkflowRef', 'No workflow')}</option>
              {activeWorkflowDefinitions.map((record) => (
                <option key={record.id} value={workflowDefinitionOptionKey(record)}>
                  {record.name} v{record.definition_version}
                </option>
              ))}
            </select>
            <select
              className="form-input agent-aware-wake-preset-select"
              value={schedulePreset}
              onChange={(event) =>
                setWakeForm({
                  ...wakeForm,
                  scheduleType: 'cron',
                  schedulePreset: event.target.value as WakeSchedulePreset,
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
                className="form-input agent-aware-wake-time-select"
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
                  className="form-input agent-aware-wake-day-select"
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
                  className="form-input agent-aware-wake-time-select"
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
                className="form-input agent-aware-wake-cron-input"
                value={wakeForm.cronExpr}
                onChange={(event) => setWakeForm({ ...wakeForm, cronExpr: event.target.value })}
                placeholder="0 9 * * *"
              />
            )}
            <div className="agent-aware-wake-submit">
              {wakeError && <span className="agent-aware-wake-error">{wakeError}</span>}
              <button className="btn btn-ghost" onClick={() => setShowCreateWake(false)}>
                {t('common.cancel', 'Cancel')}
              </button>
              <button className="btn btn-primary" onClick={createWakePolicy}>
                {t('common.create', 'Create')}
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  };

  const renderAutonomyOverview = () => {
    if (!autonomyOverview) return null;
    const triggers = autonomyOverview.triggers || [];
    const attempts = autonomyOverview.recent_attempts || [];
    const findings = autonomyOverview.findings || [];
    return (
      <>
        <div className="card agent-aware-block">
          <div className="agent-aware-overview-head">
            <div>
              <h4 className="agent-aware-h4">{t('agent.aware.autonomyTitle', 'Autonomy Control')}</h4>
              <span className="u-row u-tertiary">{t('agent.aware.autonomyDesc', 'Wake policies, attempts, and results.')}</span>
            </div>
            <button className="btn btn-primary" onClick={() => setShowCreateWake(true)}>{t('agent.aware.manualCreate', 'Manual create')}</button>
          </div>
          {findings.length > 0 && (
            <div className="agent-aware-findings">
              {findings.slice(0, 3).map((finding: any, index: number) => (
                <div key={`${finding.category || 'finding'}-${index}`} className="agent-aware-finding">
                  <strong className={stateToneClass(finding.severity === 'error' ? 'blocked' : 'waiting_approval')}>{statusLabel(finding.severity === 'error' ? 'blocked' : 'waiting_approval')}</strong>
                  {' · '}
                  {findingMessageLabel(finding) || t('agent.aware.stateUnknownState', 'Unknown state')}
                </div>
              ))}
            </div>
          )}
          {renderCreateWakeForm()}
        </div>

        <div className="card agent-aware-block">
          <div className="agent-aware-overview-head">
            <h4 className="agent-aware-h4">{t('agent.aware.wakePolicies', 'Wake Policies')}</h4>
            <span className="u-meta u-tertiary">{triggers.length}</span>
          </div>
          <div className="agent-aware-list">
            {triggers.length === 0 && <div className="u-row u-tertiary">{t('agent.aware.noTriggers')}</div>}
            {triggers.map((trigger: any) => (
              <div key={trigger.id} className="agent-aware-trigger-row">
                <div className="agent-aware-trigger-main">
                  <div className="agent-aware-min0">
                    <div className="agent-aware-trigger-tags">
                      <span className="u-meta u-tertiary">{kindLabel(trigger.display_kind)}</span>
                      {statusBadge(trigger.attention_state)}
                    </div>
                    <div className="agent-aware-trigger-title">{trigger.display_title}</div>
                    {scheduleLabelFromView(trigger) && <div className="u-row u-tertiary agent-aware-mt2">{scheduleLabelFromView(trigger)}</div>}
                    {attentionReasonLabel(trigger.attention_state) && (
                      <div className={`agent-aware-trigger-reason ${stateToneClass(trigger.attention_state)}`}>
                        {attentionReasonLabel(trigger.attention_state)}
                      </div>
                    )}
                    {trigger.last_attempt?.display_summary && <div className="agent-aware-trigger-summary">{trigger.last_attempt.display_summary}</div>}
                  </div>
                  <div className="agent-aware-trigger-actions">
                    {trigger.attention_state === 'paused' ? (
                      <button className="btn btn-ghost btn-sm" onClick={() => pauseOrResumeTrigger(trigger, true)}>{t('agent.aware.enable')}</button>
                    ) : (
                      <button className="btn btn-ghost btn-sm" onClick={() => pauseOrResumeTrigger(trigger, false)}>{t('agent.aware.disable')}</button>
                    )}
                    {trigger.last_attempt?.artifact && (
                      <button className="btn btn-ghost btn-sm" onClick={() => viewArtifact(trigger.last_attempt)}>{artifactLoading ? t('common.loading', 'Loading') : t('agent.aware.viewArtifact', 'View artifact')}</button>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="card agent-aware-block">
          <h4 className="agent-aware-h4 agent-aware-h4-mb">{t('agent.aware.attempts', 'Attempts')}</h4>
          <div className="agent-aware-attempts">
            {attempts.length === 0 && <div className="u-row u-tertiary">{t('agent.aware.noAttempts', 'No attempts yet')}</div>}
            {attempts.slice(0, 8).map((attempt: any, index: number) => (
              <div key={`${attempt.status || 'attempt'}-${index}`} className="agent-aware-attempt">
                <div className="agent-aware-min0">
                  <div className="agent-aware-attempt-title">{attempt.display_summary || attemptReasonLabel(attempt.status) || statusLabel(attempt.status)}</div>
                  {attemptReasonLabel(attempt.status) && attempt.display_summary && (
                    <div className="u-meta u-tertiary agent-aware-mt2">{attemptReasonLabel(attempt.status)}</div>
                  )}
                </div>
                {statusBadge(attempt.status)}
              </div>
            ))}
          </div>
        </div>

        {artifactView && (
          <div className="card agent-aware-block">
            <div className="agent-aware-artifact-head">
              <h4 className="agent-aware-h4">{artifactView.title || t('agent.aware.artifact', 'Artifact')}</h4>
              <button className="btn btn-ghost btn-sm" onClick={() => setArtifactView(null)}>{t('common.close', 'Close')}</button>
            </div>
            {artifactView.summary && <div className="agent-aware-artifact-summary">{artifactView.summary}</div>}
            <pre className="agent-aware-artifact-pre">{artifactView.final_reply}</pre>
          </div>
        )}
      </>
    );
  };

  if (autonomyOverview) {
    return (
      <div className="agent-aware-root">
        <TeamMemorySummaryCard agentId={agentId} section="aware" />
        <PlanQueueSection agentId={agentId} />
        {renderAutonomyOverview()}
      </div>
    );
  }

  const totalPages = Math.ceil(reflectionSessions.length / REFLECTIONS_PAGE_SIZE);
  const pageStart = reflectionPage * REFLECTIONS_PAGE_SIZE;
  const visibleSessions = reflectionSessions.slice(pageStart, pageStart + REFLECTIONS_PAGE_SIZE);

  return (
    <div className="agent-aware-root">
      <TeamMemorySummaryCard agentId={agentId} section="aware" />
      <PlanQueueSection agentId={agentId} />

      {hasStandalone && (
        <div className="card agent-aware-block">
          <div className="agent-aware-overview-head">
            <div>
              <h4 className="agent-aware-h4">{t('agent.aware.standaloneTriggers')}</h4>
            </div>
            <span className="u-meta u-tertiary">
              {standaloneTriggers.length} trigger{standaloneTriggers.length > 1 ? 's' : ''}
            </span>
          </div>
          <div className="agent-aware-compact-list">
            {[...standaloneTriggers]
              .sort((a: any, b: any) => (b.is_enabled ? 1 : 0) - (a.is_enabled ? 1 : 0))
              .slice(0, showAllTriggers ? undefined : SECTION_PAGE_SIZE)
              .map((trigger: any) => (
                <div
                  key={trigger.id}
                  className={trigger.is_enabled ? 'agent-aware-standalone-row' : 'agent-aware-standalone-row is-disabled'}
                >
                  <div className="agent-aware-flex1">
                    <div className="agent-aware-standalone-title">{triggerToHuman(trigger)}</div>
                    {trigger.reason && <div className="u-meta u-tertiary agent-aware-mt2">{trigger.reason}</div>}
                    <div className="u-tiny u-tertiary u-mono agent-aware-mt2">
                      {trigger.name}
                      {trigger.type === 'cron' ? ` · ${trigger.config?.expr}` : ''}
                    </div>
                  </div>
                  <span className="u-meta u-tertiary agent-aware-nowrap">{t('agent.aware.fired', { count: trigger.fire_count })}</span>
                  {!trigger.is_enabled && <span className="u-tiny u-tertiary">{t('agent.aware.disabled')}</span>}
                  <div className="agent-aware-row-actions">
                    <button
                      className="btn btn-ghost btn-sm"
                      onClick={async () => {
                        await triggerApi.update(agentId, trigger.id, { is_enabled: !trigger.is_enabled });
                        await onRefetchTriggers();
                      }}
                    >
                      {trigger.is_enabled ? t('agent.aware.disable') : t('agent.aware.enable')}
                    </button>
                    <button
                      className="btn btn-ghost btn-sm agent-aware-danger"
                      onClick={async () => {
                        const confirmed = await requestAppConfirm({
                          title: t('common.delete', 'Delete'),
                          message: t('agent.aware.deleteTriggerConfirm', { name: trigger.name }),
                          confirmLabel: t('common.delete', 'Delete'),
                          danger: true,
                        });
                        if (!confirmed) return;
                        await triggerApi.delete(agentId, trigger.id);
                        await onRefetchTriggers();
                      }}
                    >
                      {t('common.delete', 'Delete')}
                    </button>
                  </div>
                </div>
              ))}
          </div>
          {standaloneTriggers.length > SECTION_PAGE_SIZE && (
            <button
              className="btn btn-ghost agent-aware-showmore"
              onClick={(event) => {
                const collapse = showAllTriggers;
                onSetShowAllTriggers(!showAllTriggers);
                if (collapse) event.currentTarget.closest('.card')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
              }}
            >
              {showAllTriggers
                ? i18n.language?.startsWith('zh')
                  ? '收起'
                  : 'Show less'
                : i18n.language?.startsWith('zh')
                  ? `显示更多 ${standaloneTriggers.length - SECTION_PAGE_SIZE} 项...`
                  : `Show ${standaloneTriggers.length - SECTION_PAGE_SIZE} more...`}
            </button>
          )}
        </div>
      )}

      {reflectionSessions.length > 0 && (
        <div className="card">
          <div className="agent-aware-overview-head">
            <div>
              <h4 className="agent-aware-h4">{t('agent.aware.reflections')}</h4>
              <span className="u-row u-tertiary">{t('agent.aware.reflectionsDesc')}</span>
            </div>
            <span className="u-meta u-tertiary">
              {reflectionSessions.length} session{reflectionSessions.length > 1 ? 's' : ''}
            </span>
          </div>
          <div className="agent-aware-compact-list">
            {visibleSessions.map((session: any) => {
              const isExpanded = expandedReflection === session.id;
              const messages = reflectionMessages[session.id] || [];
              return (
                <div key={session.id} className="agent-aware-reflection-row">
                  <div
                    onClick={async () => {
                      if (isExpanded) {
                        onSetExpandedReflection(null);
                        return;
                      }
                      onSetExpandedReflection(session.id);
                      await loadReflectionMessages(session.id);
                    }}
                    className="agent-aware-reflection-head"
                  >
                    <div className="agent-aware-reflection-dot" />
                    <div className="agent-aware-reflection-body">
                      <div className="agent-aware-reflection-title">
                        {(session.title || 'Trigger execution').replace(/^🤖\s*/, '')}
                      </div>
                      <div className="agent-aware-reflection-time">
                        {new Date(session.created_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
                        {session.message_count > 0 && ` · ${session.message_count} msg`}
                      </div>
                    </div>
                    <span className={isExpanded ? 'agent-aware-chevron is-expanded' : 'agent-aware-chevron'}>
                      &#9654;
                    </span>
                  </div>
                  {isExpanded && (
                    <div className="agent-aware-reflection-detail">
                      {messages.length === 0 ? (
                        <div className="agent-aware-reflection-loading">Loading...</div>
                      ) : (
                        <div className="agent-aware-messages">
                          {messages.map((message: any, messageIndex: number) => {
                            if (message.role === 'tool_call') {
                              const toolName = message.toolName || (() => { try { return JSON.parse(message.content || '{}').name; } catch { return ''; } })() || 'tool';
                              const toolArgs = message.toolArgs || (() => { try { return JSON.parse(message.content || '{}').args; } catch { return {}; } })();
                              const toolResult = message.toolResult || '';
                              const normalizedResult = normalizeToolCallResult(toolName, toolResult);
                              const argsText = typeof toolArgs === 'string' ? toolArgs : JSON.stringify(toolArgs || {}, null, 2);
                              const resultText = normalizedResult.raw;
                              const hasDetail = argsText.length > 60 || resultText;
                              const ContainerTag = hasDetail ? 'details' : 'div';
                              const HeaderTag = hasDetail ? 'summary' : 'div';
                              return (
                                <ContainerTag key={messageIndex} className="agent-aware-tool">
                                  <HeaderTag className={hasDetail ? 'agent-aware-tool-head is-clickable' : 'agent-aware-tool-head'}>
                                    {hasDetail && <span className="agent-aware-caret">&#9654;</span>}
                                    <span className="agent-aware-tool-name">
                                      {toolName}
                                    </span>
                                    <span className="agent-aware-tool-args">
                                      {argsText.replace(/\n/g, ' ').substring(0, 60)}
                                      {argsText.length > 60 ? '...' : ''}
                                    </span>
                                  </HeaderTag>
                                  {hasDetail && (
                                    <div className="agent-aware-tool-detail">
                                      {argsText}
                                      {resultText && (
                                        <>
                                          <div className="agent-aware-tool-sep" />
                                          {normalizedResult.toolMeta ? (
                                            <StructuredToolResultBody
                                              toolName={toolName}
                                              toolMeta={normalizedResult.toolMeta}
                                              toolResult={normalizedResult.displayResult}
                                              toolRawResult={normalizedResult.raw}
                                            />
                                          ) : (
                                            <>
                                              <span className="u-tertiary">→ </span>
                                              {resultText.substring(0, 500)}
                                            </>
                                          )}
                                        </>
                                      )}
                                    </div>
                                  )}
                                </ContainerTag>
                              );
                            }

                            if (message.role === 'tool_result') {
                              const toolName = message.toolName || (() => { try { return JSON.parse(message.content || '{}').name; } catch { return ''; } })() || 'result';
                              const toolResult = message.toolResult || message.content || '';
                              const normalizedResult = normalizeToolCallResult(toolName, toolResult);
                              const resultText = normalizedResult.raw;
                              if (!resultText) return null;
                              return (
                                <details key={messageIndex} className="agent-aware-tool">
                                  <summary className="agent-aware-tool-head is-clickable">
                                    <span className="agent-aware-caret">&#9654;</span>
                                    <span className="agent-aware-tool-name">
                                      {toolName}
                                    </span>
                                    <span className="agent-aware-tool-args">
                                      → {resultText.replace(/\n/g, ' ').substring(0, 80)}
                                    </span>
                                  </summary>
                                  <div className="agent-aware-tool-detail">
                                    {normalizedResult.toolMeta ? (
                                      <StructuredToolResultBody
                                        toolName={toolName}
                                        toolMeta={normalizedResult.toolMeta}
                                        toolResult={normalizedResult.displayResult}
                                        toolRawResult={normalizedResult.raw}
                                      />
                                    ) : (
                                      resultText.substring(0, 1000)
                                    )}
                                  </div>
                                </details>
                              );
                            }

                            if (message.role === 'assistant') {
                              return (
                                <div key={messageIndex} className="agent-aware-msg-assistant">
                                  {message.content}
                                </div>
                              );
                            }

                            if (message.role === 'user') {
                              return (
                                <div key={messageIndex} className="agent-aware-msg-user">
                                  {(message.content || '').substring(0, 300)}
                                </div>
                              );
                            }

                            return null;
                          })}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
          {totalPages > 1 && (
            <div className="agent-aware-pagination">
              <button
                className="btn btn-ghost"
                disabled={reflectionPage === 0}
                onClick={() => {
                  onSetReflectionPage((previous) => Math.max(0, previous - 1));
                  onSetExpandedReflection(null);
                }}
              >
                {i18n.language?.startsWith('zh') ? '上一页' : 'Prev'}
              </button>
              <span className="agent-aware-page-count">
                {reflectionPage + 1} / {totalPages}
              </span>
              <button
                className="btn btn-ghost"
                disabled={reflectionPage >= totalPages - 1}
                onClick={() => {
                  onSetReflectionPage((previous) => Math.min(totalPages - 1, previous + 1));
                  onSetExpandedReflection(null);
                }}
              >
                {i18n.language?.startsWith('zh') ? '下一页' : 'Next'}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
