import { IconSettings } from '@tabler/icons-react';

import SurfaceLayout from '../shared/SurfaceLayout';

export default function AdminLayout() {
  return (
    <SurfaceLayout
      headingKey="nav.platformSettings"
      headingFallback="Platform Settings"
      navItems={[
        {
          to: '/admin/platform-settings',
          labelKey: 'nav.platformSettings',
          fallbackLabel: 'Platform Settings',
          icon: <IconSettings size={16} stroke={1.5} />,
          end: true,
        },
      ]}
    />
  );
}
