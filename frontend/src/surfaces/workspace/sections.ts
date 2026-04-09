export type WorkspaceSectionTab =
  | 'info'
  | 'llm'
  | 'memory'
  | 'hr'
  | 'tools'
  | 'skills'
  | 'quotas'
  | 'users'
  | 'org'
  | 'approvals'
  | 'audit'
  | 'invites';

export interface WorkspaceSection {
  tab: WorkspaceSectionTab;
  slug: string;
  path: string;
  labelKey: string;
  fallbackLabel: string;
}

export const WORKSPACE_SECTIONS: WorkspaceSection[] = [
  { tab: 'info', slug: 'info', path: '/enterprise/info', labelKey: 'enterprise.tabs.info', fallbackLabel: 'Company Info' },
  { tab: 'llm', slug: 'llm', path: '/enterprise/llm', labelKey: 'enterprise.tabs.llm', fallbackLabel: 'Models' },
  { tab: 'memory', slug: 'memory', path: '/enterprise/memory', labelKey: 'enterprise.tabs.memory', fallbackLabel: 'Memory' },
  { tab: 'hr', slug: 'hr', path: '/enterprise/hr', labelKey: 'enterprise.tabs.hr', fallbackLabel: 'HR Agent' },
  { tab: 'tools', slug: 'tools', path: '/enterprise/tools', labelKey: 'enterprise.tabs.tools', fallbackLabel: 'Tools' },
  { tab: 'skills', slug: 'skills', path: '/enterprise/skills', labelKey: 'enterprise.tabs.skills', fallbackLabel: 'Skills' },
  { tab: 'quotas', slug: 'quotas', path: '/enterprise/quotas', labelKey: 'enterprise.tabs.quotas', fallbackLabel: 'Quotas' },
  { tab: 'users', slug: 'users', path: '/enterprise/users', labelKey: 'enterprise.tabs.users', fallbackLabel: 'Users' },
  { tab: 'org', slug: 'org', path: '/enterprise/org', labelKey: 'enterprise.tabs.org', fallbackLabel: 'Org Structure' },
  { tab: 'approvals', slug: 'approvals', path: '/enterprise/approvals', labelKey: 'enterprise.tabs.approvals', fallbackLabel: 'Approvals' },
  { tab: 'audit', slug: 'audit', path: '/enterprise/audit', labelKey: 'enterprise.tabs.audit', fallbackLabel: 'Audit Log' },
  { tab: 'invites', slug: 'invitations', path: '/enterprise/invitations', labelKey: 'enterprise.tabs.invites', fallbackLabel: 'Invitation Codes' },
];

export const WORKSPACE_DEFAULT_PATH = WORKSPACE_SECTIONS[0].path;

export const WORKSPACE_LEGACY_REDIRECTS = [
  { from: '/enterprise', to: WORKSPACE_DEFAULT_PATH },
  { from: '/invitations', to: '/enterprise/invitations' },
] as const;
