import type { WorkflowDefinitionRecord } from '../../api/domains/workflows';

export type WakeSchedulePreset = 'hourly' | 'daily' | 'weekly' | 'custom';

export type WakeFormState = {
  mode: string;
  name: string;
  reason: string;
  scheduleType: string;
  schedulePreset?: WakeSchedulePreset;
  dailyTime?: string;
  weeklyDay?: string;
  weeklyTime?: string;
  cronExpr: string;
  intervalMinutes: number;
  onceAt: string;
  eventType: string;
  maxFires: number;
  expiresAt: string;
  workflowDefinitionKey: string;
  workflowArgsText: string;
};

/** A previously selected workflow template no longer resolves (deprecated,
 * revoked, or deleted) — distinct from an args JSON error so the UI can tell
 * the user to re-pick instead of blaming their JSON. */
export class StaleWorkflowRefError extends Error {
  constructor() {
    super('Selected workflow definition is no longer available.');
    this.name = 'StaleWorkflowRefError';
  }
}

export class WakeScheduleError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'WakeScheduleError';
  }
}

export function workflowDefinitionOptionKey(record: WorkflowDefinitionRecord): string {
  return `${record.name}::${record.definition_version}::${record.definition_hash}`;
}

export function workflowDefinitionFromKey(
  key: string,
  records: WorkflowDefinitionRecord[],
): WorkflowDefinitionRecord | undefined {
  return records.find((record) => workflowDefinitionOptionKey(record) === key);
}

function cronTimeParts(value: string | undefined, fallback: string): { hour: number; minute: number } {
  const raw = (value || fallback).trim();
  const match = /^(\d{1,2}):(\d{2})$/.exec(raw);
  if (!match) throw new WakeScheduleError(`Invalid time: ${raw}`);
  const hour = Number(match[1]);
  const minute = Number(match[2]);
  if (!Number.isInteger(hour) || !Number.isInteger(minute) || hour < 0 || hour > 23 || minute < 0 || minute > 59) {
    throw new WakeScheduleError(`Invalid time: ${raw}`);
  }
  return { hour, minute };
}

export function buildWakeCronExpression(wakeForm: WakeFormState): string {
  const preset = wakeForm.schedulePreset || 'custom';
  if (preset === 'hourly') return '0 * * * *';
  if (preset === 'daily') {
    const { hour, minute } = cronTimeParts(wakeForm.dailyTime, '09:00');
    return `${minute} ${hour} * * *`;
  }
  if (preset === 'weekly') {
    const { hour, minute } = cronTimeParts(wakeForm.weeklyTime, '09:00');
    const day = String(wakeForm.weeklyDay || '1');
    if (!/^[0-7]$/.test(day)) throw new WakeScheduleError(`Invalid weekday: ${day}`);
    return `${minute} ${hour} * * ${day}`;
  }
  if (!wakeForm.cronExpr.trim()) throw new WakeScheduleError('Custom schedule is required.');
  return wakeForm.cronExpr;
}

export function buildWakePolicyPayload(
  wakeForm: WakeFormState,
  selectedWorkflow?: WorkflowDefinitionRecord,
): Record<string, unknown> {
  const config: Record<string, unknown> = {};
  let type = wakeForm.scheduleType;
  if (wakeForm.mode === 'event_wait') {
    type = wakeForm.eventType;
    config.trigger_class = 'event_wait';
    if (wakeForm.eventType === 'on_message') config.reply_to_current_sender = true;
    if (wakeForm.eventType === 'poll') config.url = '';
    if (wakeForm.maxFires) config.max_fires = wakeForm.maxFires;
  } else {
    config.trigger_class = wakeForm.mode;
    if (wakeForm.scheduleType === 'cron') config.expr = buildWakeCronExpression(wakeForm);
    if (wakeForm.scheduleType === 'interval') config.minutes = wakeForm.intervalMinutes;
    if (wakeForm.scheduleType === 'once') config.at = wakeForm.onceAt;
  }
  if (wakeForm.workflowDefinitionKey && !selectedWorkflow) {
    throw new StaleWorkflowRefError();
  }
  if (selectedWorkflow) {
    const args = JSON.parse(wakeForm.workflowArgsText || '{}') as Record<string, unknown>;
    config.workflow_ref = {
      definition_name: selectedWorkflow.name,
      definition_version: selectedWorkflow.definition_version,
      definition_hash: selectedWorkflow.definition_hash,
      args,
    };
  }
  return {
    name: wakeForm.name || `wake_${Date.now()}`,
    type,
    config,
    reason: wakeForm.reason || wakeForm.name || 'Autonomous wake policy',
    max_fires: wakeForm.mode === 'event_wait' ? wakeForm.maxFires : undefined,
    expires_at: wakeForm.expiresAt || undefined,
  };
}
