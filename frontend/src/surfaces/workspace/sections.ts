export type WorkspaceSectionTab =
  | 'dashboard'
  | 'info'
  | 'llm'
  | 'memory'
  | 'knowledge'
  | 'digital_employees'
  | 'hr'
  | 'extensions'
  | 'runtime_budgets'
  | 'quotas'
  | 'users'
  | 'org'
  | 'approvals'
  | 'audit'
  | 'guard_policy'
  | 'invites';

export type WorkspaceSettingsSectionTab = Exclude<WorkspaceSectionTab, 'dashboard' | 'knowledge'>;

export interface WorkspaceSection {
  tab: WorkspaceSectionTab;
  slug: string;
  path: string;
  labelKey: string;
  fallbackLabel: string;
}

export const WORKSPACE_SECTIONS: WorkspaceSection[] = [
  { tab: 'dashboard', slug: 'dashboard', path: '/enterprise/dashboard', labelKey: 'enterprise.tabs.dashboard', fallbackLabel: 'Workbench' },
  { tab: 'info', slug: 'info', path: '/enterprise/info', labelKey: 'enterprise.tabs.info', fallbackLabel: 'Company Info' },
  { tab: 'llm', slug: 'llm', path: '/enterprise/llm', labelKey: 'enterprise.tabs.llm', fallbackLabel: 'Models' },
  { tab: 'memory', slug: 'memory', path: '/enterprise/memory', labelKey: 'enterprise.tabs.memory', fallbackLabel: 'Memory' },
  { tab: 'knowledge', slug: 'knowledge', path: '/enterprise/knowledge', labelKey: 'enterprise.tabs.knowledge', fallbackLabel: 'Company Knowledge' },
  { tab: 'digital_employees', slug: 'digital-employees', path: '/enterprise/digital-employees', labelKey: 'enterprise.tabs.digitalEmployees', fallbackLabel: 'Digital Employees' },
  { tab: 'hr', slug: 'hr', path: '/enterprise/hr', labelKey: 'enterprise.tabs.hr', fallbackLabel: 'HR Agent' },
  { tab: 'extensions', slug: 'extensions', path: '/enterprise/extensions', labelKey: 'enterprise.tabs.extensions', fallbackLabel: 'Extensions' },
  { tab: 'runtime_budgets', slug: 'runtime-budgets', path: '/enterprise/runtime-budgets', labelKey: 'enterprise.tabs.runtimeBudgets', fallbackLabel: 'Runtime Budgets' },
  { tab: 'quotas', slug: 'quotas', path: '/enterprise/quotas', labelKey: 'enterprise.tabs.quotas', fallbackLabel: 'Quotas' },
  { tab: 'users', slug: 'users', path: '/enterprise/users', labelKey: 'enterprise.tabs.users', fallbackLabel: 'Users' },
  { tab: 'org', slug: 'org', path: '/enterprise/org', labelKey: 'enterprise.tabs.org', fallbackLabel: 'Org Structure' },
  { tab: 'approvals', slug: 'approvals', path: '/enterprise/approvals', labelKey: 'enterprise.tabs.approvals', fallbackLabel: 'Approvals' },
  { tab: 'audit', slug: 'audit', path: '/enterprise/audit', labelKey: 'enterprise.tabs.audit', fallbackLabel: 'Audit Log' },
  { tab: 'guard_policy', slug: 'action-guardrails', path: '/enterprise/action-guardrails', labelKey: 'enterprise.tabs.guardPolicy', fallbackLabel: 'Action Guardrails' },
  { tab: 'invites', slug: 'invitations', path: '/enterprise/invitations', labelKey: 'enterprise.tabs.invites', fallbackLabel: 'Invitation Codes' },
];

export const WORKSPACE_SETTINGS_SECTIONS = WORKSPACE_SECTIONS.filter(
  (section): section is WorkspaceSection & { tab: WorkspaceSettingsSectionTab } =>
    section.tab !== 'dashboard' && section.tab !== 'knowledge',
);

export function workspaceSectionsForRole(role: string | undefined): WorkspaceSection[] {
  // PDEC-013: both administrator roles operate the complete selected company's
  // workspace. A platform administrator's company context is the authenticated
  // selected company (X-Tenant-Id); the server stays authoritative and answers
  // with a typed selection/not-found response when no valid company is
  // selected, so no client-side tab subset is a second authority boundary.
  if (role === 'org_admin' || role === 'platform_admin') return WORKSPACE_SECTIONS;
  return [];
}

export function canRoleAccessWorkspaceSection(role: string | undefined, tab: WorkspaceSectionTab): boolean {
  return workspaceSectionsForRole(role).some((section) => section.tab === tab);
}

export const WORKSPACE_DEFAULT_PATH = WORKSPACE_SECTIONS[0].path;

export const WORKSPACE_LEGACY_REDIRECTS = [
  { from: '/enterprise', to: WORKSPACE_DEFAULT_PATH },
  { from: '/invitations', to: '/enterprise/invitations' },
  { from: '/enterprise/tools', to: '/enterprise/extensions' },
  { from: '/enterprise/skills', to: '/enterprise/extensions' },
  { from: '/enterprise/subagents', to: '/enterprise/extensions' },
] as const;
