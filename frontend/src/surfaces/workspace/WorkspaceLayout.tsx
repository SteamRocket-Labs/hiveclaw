import { IconBook, IconBrain, IconChecklist, IconFileText, IconLayoutDashboard, IconRobot, IconShieldCheck, IconSitemap, IconUserStar, IconUsers } from '@tabler/icons-react';

import SurfaceLayout from '../shared/SurfaceLayout';
import { useAuthStore } from '../../stores';
import { workspaceSectionsForRole } from './sections';

const ICONS = {
  dashboard: <IconLayoutDashboard size={16} stroke={1.5} />,
  info: <IconFileText size={16} stroke={1.5} />,
  llm: <IconRobot size={16} stroke={1.5} />,
  memory: <IconBrain size={16} stroke={1.5} />,
  knowledge: <IconBook size={16} stroke={1.5} />,
  digital_employees: <IconUsers size={16} stroke={1.5} />,
  hr: <IconUserStar size={16} stroke={1.5} />,
  extensions: <IconSitemap size={16} stroke={1.5} />,
  runtime_budgets: <IconShieldCheck size={16} stroke={1.5} />,
  quotas: <IconChecklist size={16} stroke={1.5} />,
  users: <IconUsers size={16} stroke={1.5} />,
  org: <IconUsers size={16} stroke={1.5} />,
  approvals: <IconShieldCheck size={16} stroke={1.5} />,
  audit: <IconFileText size={16} stroke={1.5} />,
  guard_policy: <IconShieldCheck size={16} stroke={1.5} />,
  invites: <IconChecklist size={16} stroke={1.5} />,
} as const;

export default function WorkspaceLayout() {
  const role = useAuthStore((state) => state.user?.role);
  return (
    <SurfaceLayout
      headingKey={role === 'platform_admin' ? 'nav.superAdmin' : 'nav.enterprise'}
      headingFallback={role === 'platform_admin' ? 'Platform Admin' : 'Company Admin'}
      navItems={workspaceSectionsForRole(role).map((section) => ({
        to: section.path,
        labelKey: section.labelKey,
        fallbackLabel: section.fallbackLabel,
        icon: ICONS[section.tab],
      }))}
    />
  );
}
