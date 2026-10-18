import { get, post, patch } from '../core';

export interface ProjectScope { userId: string; tenantId: string }
export interface ProjectSummary {
  id: string; title: string; revision: number; owner_user_id: string | null;
  archived: boolean; can_manage: boolean; can_write: boolean;
}
export interface ProjectEntry {
  id: string; target_kind: 'workspace_file' | 'session'; section: string; removed: boolean;
  state: 'linked' | 'denied' | 'not_found' | 'needs_reconciliation';
  agent_id?: string; path?: string; session_id?: string; title?: string;
}
export interface ProjectDetail extends ProjectSummary {
  entries: ProjectEntry[]; next_offset: number | null;
  grants?: Array<{ id: string; user_id: string; actions: string[]; recipient_label?: string | null }>;
}
function url(scope: ProjectScope, suffix = '', extra: Record<string, string> = {}) {
  return `/projects${suffix}?${new URLSearchParams({ expected_user_id: scope.userId,
    expected_tenant_id: scope.tenantId, ...extra })}`;
}
export const projectsApi = {
  list: (scope: ProjectScope, offset = 0, includeArchived = false) =>
    get<{ projects: ProjectSummary[]; next_offset: number | null }>(url(scope, '', {
      offset: String(offset), include_archived: String(includeArchived) })),
  read: (scope: ProjectScope, id: string, offset = 0, includeRemoved = false) =>
    get<ProjectDetail>(url(scope, `/${id}`, { offset: String(offset), include_removed: String(includeRemoved) })),
  readEntry: (scope: ProjectScope, id: string, entryId: string) =>
    get<ProjectEntry>(url(scope, `/${id}/entries/${entryId}`)),
  create: (scope: ProjectScope, body: { id: string; title: string }) => post<ProjectSummary>(url(scope), body),
  edit: (scope: ProjectScope, id: string, body: { expected_revision: number; title?: string; archived?: boolean; owner_email?: string }) =>
    patch(url(scope, `/${id}`), body),
  add: (scope: ProjectScope, id: string, body: { id: string; expected_revision: number; target_kind: 'workspace_file' | 'session';
    agent_id: string; path?: string; session_id?: string; section: string }) => post(url(scope, `/${id}/entries`), body),
  editEntry: (scope: ProjectScope, id: string, entryId: string, body: { expected_revision: number; section?: string; removed?: boolean }) =>
    patch(url(scope, `/${id}/entries/${entryId}`), body),
  grant: (scope: ProjectScope, id: string, body: { id: string; expected_revision: number; email: string; role: 'reader' | 'editor' }) =>
    post(url(scope, `/${id}/grants`), body),
  revoke: (scope: ProjectScope, id: string, grantId: string, revision: number) =>
    post(url(scope, `/${id}/grants/${grantId}/revoke`), { expected_revision: revision }),
};
