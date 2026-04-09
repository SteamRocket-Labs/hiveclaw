import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import WorkspaceApprovalsSection from './WorkspaceApprovalsSection';
import WorkspaceAuditSection from './WorkspaceAuditSection';
import WorkspaceInvitesSection from './WorkspaceInvitesSection';
import WorkspaceOrgSection from './WorkspaceOrgSection';
import WorkspaceQuotasSection from './WorkspaceQuotasSection';
import WorkspaceSkillsSection from './WorkspaceSkillsSection';
import WorkspaceToolsSection from './WorkspaceToolsSection';
import WorkspaceUsersSection from './WorkspaceUsersSection';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallbackOrOptions?: string | Record<string, unknown>, options?: Record<string, unknown>) => {
      if (typeof fallbackOrOptions === 'string') {
        return fallbackOrOptions.replace(/\{\{\s*(\w+)\s*\}\}/g, (_, name) => String(options?.[name] ?? ''));
      }
      if (key === 'enterprise.audit.records') {
        return `records:${(fallbackOrOptions as Record<string, unknown> | undefined)?.count ?? options?.count ?? 0}`;
      }
      return key.split('.').pop() ?? key;
    },
  }),
}));

vi.mock('@tanstack/react-query', () => ({
  useQuery: ({ queryKey }: { queryKey: unknown[] }) => {
    const key = String(queryKey[0]);
    if (key === 'approvals') {
      return {
        data: [
          {
            id: 'approval-1',
            action_type: 'deploy_run',
            agent_id: 'agent-12345678',
            agent_name: 'Deploy Bot',
            created_at: '2026-03-27T09:00:00Z',
            status: 'pending',
          },
        ],
      };
    }
    if (key === 'audit-logs') {
      return {
        data: [
          {
            id: 'audit-1',
            action: 'supervision_tick',
            created_at: '2026-03-27T09:00:00Z',
            agent_id: 'agent-12345678',
            details: { job: 'nightly' },
          },
        ],
      };
    }
    if (key === 'org-departments') {
      return {
        data: [
          { id: 'dept-1', name: 'Engineering', parent_id: null, member_count: 1 },
        ],
      };
    }
    if (key === 'org-members') {
      return {
        data: [
          { id: 'member-1', name: 'Alice', title: 'Engineer', department_path: 'Engineering', email: 'alice@example.com' },
        ],
      };
    }
    if (key === 'feishu-runtime-status') {
      return {
        data: {
          ok: true,
          scope: 'global',
          message: 'Feishu CLI is ready.',
          cli_enabled: true,
          cli_available: true,
          cli_bin: 'lark-cli',
          docs_read_ready: true,
          base_tasks_ready: true,
        },
      };
    }
    return { data: null };
  },
  useMutation: () => ({
    mutate: vi.fn(),
  }),
  useQueryClient: () => ({
    invalidateQueries: vi.fn(),
  }),
}));

vi.mock('../../components/FileBrowser', () => ({
  default: ({ title }: { title?: string }) => <div>{title || 'File Browser Mock'}</div>,
}));

vi.mock('../UserManagement', () => ({
  default: () => <div>User Management Mock</div>,
}));

vi.mock('../InvitationCodes', () => ({
  default: () => <div>Invitation Codes Mock</div>,
}));

describe('Workspace remaining sections', () => {
  it('renders the approvals and audit sections as standalone workspace modules', () => {
    const approvalsMarkup = renderToStaticMarkup(
      <WorkspaceApprovalsSection selectedTenantId="tenant-1" />,
    );
    const auditMarkup = renderToStaticMarkup(
      <WorkspaceAuditSection selectedTenantId="tenant-1" />,
    );

    expect(approvalsMarkup).toContain('deploy_run');
    expect(auditMarkup).toContain('supervision_tick');
    expect(auditMarkup).toContain('records:1');
  });

  it('renders the org, quotas, and tools sections outside of EnterpriseSettings', () => {
    const orgMarkup = renderToStaticMarkup(
      <WorkspaceOrgSection selectedTenantId="tenant-1" />,
    );
    const quotasMarkup = renderToStaticMarkup(<WorkspaceQuotasSection />);
    const toolsMarkup = renderToStaticMarkup(
      <WorkspaceToolsSection selectedTenantId="tenant-1" />,
    );

    expect(orgMarkup).toContain('Engineering');
    expect(orgMarkup).toContain('Feishu Runtime Status');
    expect(orgMarkup).toContain('Base / Tasks');
    expect(quotasMarkup).toContain('Employee Token Quotas');
    expect(quotasMarkup).toContain('defaultMaxTriggers');
    expect(toolsMarkup).toContain('Global Tools');
    expect(toolsMarkup).not.toContain('Feishu Runtime Status');
  });

  it('renders users, invitations, and skills as independent workspace sections', () => {
    const usersMarkup = renderToStaticMarkup(
      <WorkspaceUsersSection selectedTenantId="tenant-1" />,
    );
    const invitesMarkup = renderToStaticMarkup(<WorkspaceInvitesSection />);
    const skillsMarkup = renderToStaticMarkup(<WorkspaceSkillsSection />);

    expect(usersMarkup).toContain('User Management Mock');
    expect(invitesMarkup).toContain('Invitation Codes Mock');
    expect(skillsMarkup).toContain('Skill Registry');
    expect(skillsMarkup).toContain('Skill Files');
  });
});
