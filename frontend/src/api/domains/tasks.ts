/**
 * Tasks domain adapter — agent task management.
 */

import { get, post, patch } from '../core';
import type { Task } from '../../types';

export interface TaskCreateParams {
  title: string;
  description?: string;
  type?: 'todo' | 'supervision';
  priority?: 'low' | 'medium' | 'high' | 'urgent';
  assignee?: string;
  due_date?: string;
}

export interface TaskUpdateParams {
  title?: string;
  description?: string;
  status?: 'pending' | 'doing' | 'done';
  priority?: 'low' | 'medium' | 'high' | 'urgent';
}

export interface TaskLog {
  id: string;
  task_id: string;
  content: string;
  created_at: string;
}

export const taskApi = {
  list: (agentId: string) => get<Task[]>(`/agents/${agentId}/tasks/`),
  create: (agentId: string, data: TaskCreateParams) => post<Task>(`/agents/${agentId}/tasks/`, data),
  update: (agentId: string, taskId: string, data: TaskUpdateParams) =>
    patch<Task>(`/agents/${agentId}/tasks/${taskId}`, data),
  getLogs: (agentId: string, taskId: string) => get<TaskLog[]>(`/agents/${agentId}/tasks/${taskId}/logs`),
  trigger: (agentId: string, taskId: string) => post<void>(`/agents/${agentId}/tasks/${taskId}/trigger`),
};
