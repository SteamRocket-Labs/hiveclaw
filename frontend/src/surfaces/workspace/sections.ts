export type WorkspaceSectionTab =
  | 'dashboard'
  | 'info'
  | 'llm'
  | 'memory'
  | 'digital_employees'
  | 'hr'
  | 'extensions'
  | 'runtime_budgets'
  | 'quotas'
  | 'users'
  | 'org'
  | 'approvals'
  | 'audit'
  | 'invites';

export type WorkspaceSettingsSectionTab = Exclude<WorkspaceSectionTab, 'dashboard'>;

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
  { tab: 'digital_employees', slug: 'digital-employees', path: '/enterprise/digital-employees', labelKey: 'enterprise.tabs.digitalEmployees', fallbackLabel: 'Digital Employees' },
  { tab: 'hr', slug: 'hr', path: '/enterprise/hr', labelKey: 'enterprise.tabs.hr', fallbackLabel: 'HR Agent' },
  { tab: 'extensions', slug: 'extensions', path: '/enterprise/extensions', labelKey: 'enterprise.tabs.extensions', fallbackLabel: 'Extensions' },
  { tab: 'runtime_budgets', slug: 'runtime-budgets', path: '/enterprise/runtime-budgets', labelKey: 'enterprise.tabs.runtimeBudgets', fallbackLabel: 'Runtime Budgets' },
  { tab: 'quotas', slug: 'quotas', path: '/enterprise/quotas', labelKey: 'enterprise.tabs.quotas', fallbackLabel: 'Quotas' },
  { tab: 'users', slug: 'users', path: '/enterprise/users', labelKey: 'enterprise.tabs.users', fallbackLabel: 'Users' },
  { tab: 'org', slug: 'org', path: '/enterprise/org', labelKey: 'enterprise.tabs.org', fallbackLabel: 'Org Structure' },
  { tab: 'approvals', slug: 'approvals', path: '/enterprise/approvals', labelKey: 'enterprise.tabs.approvals', fallbackLabel: 'Approvals' },
  { tab: 'audit', slug: 'audit', path: '/enterprise/audit', labelKey: 'enterprise.tabs.audit', fallbackLabel: 'Audit Log' },
  { tab: 'invites', slug: 'invitations', path: '/enterprise/invitations', labelKey: 'enterprise.tabs.invites', fallbackLabel: 'Invitation Codes' },
];

export const WORKSPACE_SETTINGS_SECTIONS = WORKSPACE_SECTIONS.filter(
  (section): section is WorkspaceSection & { tab: WorkspaceSettingsSectionTab } => section.tab !== 'dashboard',
);

export const WORKSPACE_DEFAULT_PATH = WORKSPACE_SECTIONS[0].path;

export const WORKSPACE_LEGACY_REDIRECTS = [
  { from: '/enterprise', to: WORKSPACE_DEFAULT_PATH },
  { from: '/invitations', to: '/enterprise/invitations' },
  { from: '/enterprise/tools', to: '/enterprise/extensions' },
  { from: '/enterprise/skills', to: '/enterprise/extensions' },
  { from: '/enterprise/subagents', to: '/enterprise/extensions' },
] as const;
