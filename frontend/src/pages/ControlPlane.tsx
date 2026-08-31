import type { ReactNode } from 'react';
import { Link, Navigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  IconBook,
  IconBrain,
  IconBuilding,
  IconChartBar,
  IconDatabase,
  IconDeviceDesktop,
  IconFileText,
  IconPlugConnected,
  IconShieldCheck,
  IconShieldHalfFilled,
  IconUsers,
} from '@tabler/icons-react';

import { enterpriseApi } from '../api/domains/enterprise';
import { useAuthStore } from '../stores';
import {
  canRoleAccessWorkspaceSection,
  WORKSPACE_SETTINGS_SECTIONS,
  workspaceSectionsForRole,
  type WorkspaceSettingsSectionTab,
} from '../surfaces/workspace/sections';
import EnterpriseSettings from './EnterpriseSettings';

type ControlPlaneTab = WorkspaceSettingsSectionTab;

interface ControlPlaneProps {
  tab?: ControlPlaneTab;
}

interface ControlPlaneCard {
  tab?: ControlPlaneTab;
  to: string;
  title: string;
  description: string;
  translationKey?: string;
  icon: ReactNode;
  group: 'workspace' | 'governance' | 'runtime' | 'channels';
}

const CONTROL_PLANE_CARDS: ControlPlaneCard[] = [
  {
    tab: 'hr',
    to: '/enterprise/hr',
    title: 'Agent Governance',
    description: 'HR Agent setup and employee creation policy.',
    icon: <IconUsers size={18} stroke={1.6} />,
    group: 'governance',
  },
  {
    tab: 'digital_employees',
    to: '/enterprise/digital-employees',
    title: 'Digital Employees',
    description: 'Company-wide employee inventory, lifecycle controls, and admin deletion.',
    icon: <IconUsers size={18} stroke={1.6} />,
    group: 'workspace',
  },
  {
    tab: 'llm',
    to: '/enterprise/llm',
    title: 'Models & Budget',
    description: 'Provider pool, default model, reasoning controls, and budget-facing runtime knobs.',
    icon: <IconDatabase size={18} stroke={1.6} />,
    group: 'runtime',
  },
  {
    tab: 'extensions',
    to: '/enterprise/extensions',
    title: 'Extension Catalog',
    description: 'MCP servers, plugins, skills, subagents, connectors, and tenant-level capability installs.',
    icon: <IconPlugConnected size={18} stroke={1.6} />,
    group: 'runtime',
  },
  {
    tab: 'memory',
    to: '/enterprise/memory',
    title: 'Memory Governance',
    description: 'Agent memory retention, hygiene, and governed writes.',
    icon: <IconBrain size={18} stroke={1.6} />,
    group: 'governance',
  },
  {
    to: '/enterprise/knowledge',
    title: 'Company Knowledge',
    description: 'Review, publish, authorize, retire, and restore governed knowledge for employees.',
    icon: <IconBook size={18} stroke={1.6} />,
    group: 'governance',
  },
  {
    tab: 'info',
    to: '/enterprise/info',
    title: 'Channels & Integrations',
    description: 'Tenant identity, timezone, presentation, and role-appropriate company actions.',
    icon: <IconBuilding size={18} stroke={1.6} />,
    group: 'channels',
  },
  {
    tab: 'approvals',
    to: '/enterprise/approvals',
    title: 'Approval Center',
    description: 'Sensitive action review, platform-gated confirmations, and approval state.',
    icon: <IconShieldCheck size={18} stroke={1.6} />,
    group: 'governance',
  },
  {
    tab: 'audit',
    to: '/enterprise/audit',
    title: 'Audit Log',
    description: 'Tenant-scoped audit stream for runtime events, action traces, and background work.',
    icon: <IconFileText size={18} stroke={1.6} />,
    group: 'governance',
  },
  {
    tab: 'guard_policy',
    to: '/enterprise/action-guardrails',
    title: 'Action Guardrails',
    description: 'Business-level company backstops for employee actions and outbound effects.',
    translationKey: 'guardPolicy',
    icon: <IconShieldCheck size={18} stroke={1.6} />,
    group: 'governance',
  },
  {
    tab: 'users',
    to: '/enterprise/users',
    title: 'Members & Roles',
    description: 'Users, role changes, quota assignment, and organization access.',
    icon: <IconUsers size={18} stroke={1.6} />,
    group: 'workspace',
  },
  {
    tab: 'org',
    to: '/enterprise/org',
    title: 'Organization Structure',
    description: 'Departments, members, Feishu runtime readiness, and company directory shape.',
    icon: <IconChartBar size={18} stroke={1.6} />,
    group: 'workspace',
  },
  {
    tab: 'quotas',
    to: '/enterprise/quotas',
    title: 'Quotas',
    description: 'Default employee token quotas, trigger caps, and company-level usage boundaries.',
    icon: <IconShieldCheck size={18} stroke={1.6} />,
    group: 'runtime',
  },
  {
    tab: 'runtime_budgets',
    to: '/enterprise/runtime-budgets',
    title: 'Runtime Protection',
    description: 'Company-level limits that take priority over the platform defaults.',
    icon: <IconShieldHalfFilled size={18} stroke={1.6} />,
    group: 'runtime',
  },
  {
    tab: 'invites',
    to: '/enterprise/invitations',
    title: 'Invitation Codes',
    description: 'Company invites and controlled onboarding into this tenant.',
    icon: <IconFileText size={18} stroke={1.6} />,
    group: 'workspace',
  },
  {
    to: '/local-agents',
    title: 'Local Agent Channel',
    description: 'Local-runtime agents with ordinary Agent permissions, workspace transfer, direct channel chat, and Hive Connect pairing.',
    icon: <IconDeviceDesktop size={18} stroke={1.6} />,
    group: 'channels',
  },
];

function cardForTab(tab: ControlPlaneTab | undefined) {
  if (!tab) return null;
  return CONTROL_PLANE_CARDS.find((card) => card.tab === tab) ?? null;
}

function readCurrentTenantId() {
  try {
    if (typeof localStorage?.getItem !== 'function') return '';
    return localStorage.getItem('current_tenant_id') || '';
  } catch {
    return '';
  }
}

export default function ControlPlane({ tab }: ControlPlaneProps) {
  const { t } = useTranslation();
  const role = useAuthStore((state) => state.user?.role);
  const selectedTenantId = readCurrentTenantId();
  const section = cardForTab(tab);
  const { data: stats } = useQuery({
    queryKey: ['enterprise-stats', selectedTenantId],
    queryFn: () => enterpriseApi.getStats(selectedTenantId || undefined),
    enabled: role === 'org_admin',
  });

  if (tab && !canRoleAccessWorkspaceSection(role, tab)) {
    return <Navigate to="/enterprise/dashboard" replace />;
  }

  if (tab && section) {
    return (
      <div className="workbench-page control-plane-page">
        <div className="workbench-hero compact">
          <div>
            <span className="workbench-eyebrow">{t('controlPlane.sectionEyebrow', 'Control Plane')}</span>
            <h1 className="page-title">{t(`controlPlane.sections.${tab}.title`, section.title)}</h1>
            <p className="page-subtitle">{t(`controlPlane.sections.${tab}.description`, section.description)}</p>
          </div>
          <Link to="/enterprise/dashboard" className="btn btn-secondary">
            {t('controlPlane.backToOverview', 'Back to overview')}
          </Link>
        </div>
        <section className="workbench-panel control-plane-section-panel">
          <EnterpriseSettings forcedTab={tab} hideTabs chrome="embedded" />
        </section>
      </div>
    );
  }

  const allowedPaths = new Set(workspaceSectionsForRole(role).map((item) => item.path));
  const visibleCards = CONTROL_PLANE_CARDS.filter((card) =>
    card.to === '/local-agents' ? role === 'org_admin' : allowedPaths.has(card.to),
  );
  const workspaceSections = WORKSPACE_SETTINGS_SECTIONS.filter((item) =>
    canRoleAccessWorkspaceSection(role, item.tab),
  ).length;

  return (
    <div className="workbench-page control-plane-page">
      <div className="workbench-hero">
        <div>
          <span className="workbench-eyebrow">{t('controlPlane.eyebrow', 'Enterprise operating console')}</span>
          <h1 className="page-title">{t('controlPlane.title', 'Control Plane')}</h1>
          <p className="page-subtitle">
            {t(
              'controlPlane.subtitle',
              'Company-scale governance for digital employees: people, models, tools, memory, delegation, approvals, audit, and local runtime channels.',
            )}
          </p>
        </div>
        <span className="workbench-hero-icon">
          <IconChartBar size={22} stroke={1.7} />
        </span>
      </div>

      <div className="workbench-metrics">
        {role === 'org_admin' && (
          <>
            <div className="workbench-metric">
              <span>{t('controlPlane.metrics.users', 'Users')}</span>
              <strong>{stats?.total_users ?? '-'}</strong>
            </div>
            <div className="workbench-metric">
              <span>{t('controlPlane.metrics.agents', 'Employees')}</span>
              <strong>{stats ? `${stats.running_agents}/${stats.total_agents}` : '-'}</strong>
            </div>
            <div className="workbench-metric">
              <span>{t('controlPlane.metrics.approvals', 'Pending approvals')}</span>
              <strong>{stats?.pending_approvals ?? '-'}</strong>
            </div>
          </>
        )}
        <div className="workbench-metric">
          <span>{t('controlPlane.metrics.sections', 'Admin surfaces')}</span>
          <strong>{workspaceSections}</strong>
        </div>
      </div>

      <section className="workbench-panel">
        <div className="workbench-panel-header">
          <div>
            <h2>{t('controlPlane.operatingAreas', 'Operating areas')}</h2>
            <p>{t('controlPlane.operatingAreasDesc', 'Every card opens an implemented workspace section or user-scoped runtime page. No old admin capability is hidden behind legacy navigation.')}</p>
          </div>
        </div>
        <div className="control-plane-grid">
          {visibleCards.map((card) => (
            <Link key={card.to} to={card.to} className={`control-plane-card ${card.group}`}>
              <span className="control-plane-card-icon">{card.icon}</span>
              <span>
                <strong>
                  {card.translationKey
                    ? t(`controlPlane.cards.${card.translationKey}.title`, card.title)
                    : t(`controlPlane.card.${card.title}`, card.title)}
                </strong>
                <small>
                  {card.translationKey
                    ? t(`controlPlane.cards.${card.translationKey}.description`, card.description)
                    : t(`controlPlane.card.${card.title}.desc`, card.description)}
                </small>
              </span>
            </Link>
          ))}
        </div>
      </section>
    </div>
  );
}
