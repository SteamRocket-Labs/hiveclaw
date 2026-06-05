/**
 * Subagent configuration adapter (cut C2/C3, docs/subagent-source-capability.md §12).
 *
 * Two scopes, one resolution chain (agent wins → tenant library → builtin
 * read-only templates). Markdown IS the configuration format: PUT sends the
 * full 定义.md text; the backend validates it with the same parser the
 * runtime uses, so a 422 here is exactly what spawn-time would reject.
 *
 *   GET    /agents/{agentId}/subagents          merged list, rows tagged scope
 *   GET    /agents/{agentId}/subagents/{name}   definition text + effective scope + memory digest
 *   PUT    /agents/{agentId}/subagents/{name}   create/update the agent-level definition
 *   DELETE /agents/{agentId}/subagents/{name}   delete agent-level only (name falls back to tenant)
 *
 *   GET    /enterprise/subagents                tenant company library (org admin)
 *   GET    /enterprise/subagents/{name}         full definition text for the edit flow
 *   PUT    /enterprise/subagents/{name}
 *   DELETE /enterprise/subagents/{name}
 */

import { del, get, put } from '../core';

export type SubagentScope = 'agent' | 'tenant' | 'builtin';

export interface SubagentSpecSummary {
  name: string;
  type: string;
  model: string | null;
  isolation: 'none' | 'brief' | 'all';
  max_tool_rounds: number | null;
  allowed_tools: string[];
  excluded_tools: string[];
}

export interface SubagentRow extends SubagentSpecSummary {
  scope: SubagentScope;
}

export interface SubagentMemoryDigest {
  exists: boolean;
  entries: number;
}

export interface SubagentDetail {
  name: string;
  scope: SubagentScope;
  definition: string;
  spec: SubagentSpecSummary;
  memory: SubagentMemoryDigest;
}

export interface SubagentSaveResult {
  name: string;
  scope: SubagentScope;
  spec: SubagentSpecSummary;
}

export const subagentApi = {
  list: (agentId: string) => get<{ subagents: SubagentRow[] }>(`/agents/${agentId}/subagents`),
  get: (agentId: string, name: string) =>
    get<SubagentDetail>(`/agents/${agentId}/subagents/${encodeURIComponent(name)}`),
  save: (agentId: string, name: string, definition: string) =>
    put<SubagentSaveResult>(`/agents/${agentId}/subagents/${encodeURIComponent(name)}`, { definition }),
  remove: (agentId: string, name: string) =>
    del<{ deleted: string; scope: SubagentScope }>(`/agents/${agentId}/subagents/${encodeURIComponent(name)}`),

  enterpriseList: () => get<{ subagents: SubagentRow[] }>(`/enterprise/subagents`),
  enterpriseGet: (name: string) => get<SubagentDetail>(`/enterprise/subagents/${encodeURIComponent(name)}`),
  enterpriseSave: (name: string, definition: string) =>
    put<SubagentSaveResult>(`/enterprise/subagents/${encodeURIComponent(name)}`, { definition }),
  enterpriseRemove: (name: string) =>
    del<{ deleted: string; scope: SubagentScope }>(`/enterprise/subagents/${encodeURIComponent(name)}`),
};
