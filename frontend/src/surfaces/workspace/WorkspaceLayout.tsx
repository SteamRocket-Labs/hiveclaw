import type { ReactNode } from 'react';
import { IconArrowLeft, IconFileText, IconPlugConnected, IconRobot, IconShieldCheck, IconUsers } from '@tabler/icons-react';

import SurfaceLayout from '../shared/SurfaceLayout';
import { useAuthStore } from '../../stores';
import { workspaceNavItemsForRole, type WorkspaceNavTab } from './sections';

const NAV_ICONS: Record<WorkspaceNavTab, ReactNode> = {
  users: <IconUsers size={16} stroke={1.5} />,
  digital_employees: <IconRobot size={16} stroke={1.5} />,
  llm: <IconRobot size={16} stroke={1.5} />,
  info: <IconPlugConnected size={16} stroke={1.5} />,
  approvals: <IconShieldCheck size={16} stroke={1.5} />,
  audit: <IconFileText size={16} stroke={1.5} />,
};

export default function WorkspaceLayout() {
  const role = useAuthStore((state) => state.user?.role);
  return (
    <SurfaceLayout
      headingKey={role === 'platform_admin' ? 'nav.superAdmin' : 'nav.enterprise'}
      headingFallback={role === 'platform_admin' ? 'Platform Admin' : 'Company Admin'}
      navItems={[
        ...workspaceNavItemsForRole(role).map((item) => ({
          to: item.to,
          labelKey: item.labelKey,
          fallbackLabel: item.fallbackLabel,
          icon: NAV_ICONS[item.tab],
        })),
        // Both administrator roles keep a direct return to the product app
        // (the assistant conversation); the footer logout is not a return path.
        {
          to: '/',
          labelKey: 'nav.backToApp',
          fallbackLabel: 'Back to App',
          icon: <IconArrowLeft size={16} stroke={1.5} />,
          end: true,
        },
      ]}
    />
  );
}
