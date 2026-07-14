/**
 * Tasks domain adapter — agent task management.
 */

import { get, post, patch } from '../core';
import type { Task } from '../../types';

export interface TaskCreateParams {
  request_id: string;
  title: string;
  description?: string | null;
  type?: 'todo';
  priority?: 'low' | 'medium' | 'high' | 'urgent';
  assignee?: string;
  due_date?: string | null;
  confirmed_plan_id?: string;
  confirmed_plan_version?: number;
  confirmed_plan_session_id?: string;
}

export interface TaskUpdateParams {
  title?: string;
  description?: string;
  priority?: 'low' | 'medium' | 'high' | 'urgent';
}

export interface TaskTriggerParams {
  request_id: string;
  confirmed_plan_id?: string;
  confirmed_plan_version?: number;
  confirmed_plan_session_id?: string;
}

export interface TaskLog {
  id: string;
  task_id: string;
  content: string;
  created_at: string;
}

export interface BusinessTaskDetail {
  task: Task;
  logs: TaskLog[];
}

export interface TaskCancelParams {
  reason?: string;
}

export interface TaskReconcileParams {
  decision: 'retry_safe' | 'close_without_retry';
  reason: string;
}

export const taskApi = {
  list: (agentId: string) => get<Task[]>(`/agents/${agentId}/tasks/`),
  get: (agentId: string, taskId: string) => get<BusinessTaskDetail>(`/agents/${agentId}/tasks/${taskId}`),
  create: (agentId: string, data: TaskCreateParams) => post<Task>(`/agents/${agentId}/tasks/`, data),
  update: (agentId: string, taskId: string, data: TaskUpdateParams) =>
    patch<Task>(`/agents/${agentId}/tasks/${taskId}`, data),
  getLogs: (agentId: string, taskId: string) => get<TaskLog[]>(`/agents/${agentId}/tasks/${taskId}/logs`),
  trigger: (agentId: string, taskId: string, data: TaskTriggerParams) =>
    post<{ status: 'triggered'; task_id: string; runtime_task_id: string }>(
      `/agents/${agentId}/tasks/${taskId}/trigger`,
      data,
    ),
  cancel: (agentId: string, taskId: string, data: TaskCancelParams = {}) =>
    post<BusinessTaskDetail>(`/agents/${agentId}/tasks/${taskId}/cancel`, data),
  retry: (agentId: string, taskId: string, data: TaskTriggerParams) =>
    post<{ status: 'triggered'; task_id: string; runtime_task_id: string }>(
      `/agents/${agentId}/tasks/${taskId}/retry`,
      data,
    ),
  reconcile: (agentId: string, taskId: string, data: TaskReconcileParams) =>
    post<BusinessTaskDetail>(`/agents/${agentId}/tasks/${taskId}/reconcile`, data),
};
