import { IconBrain, IconChecklist, IconFileText, IconRobot, IconSettings, IconShieldCheck, IconUserStar, IconUsers } from '@tabler/icons-react';

import SurfaceLayout from '../shared/SurfaceLayout';
import { WORKSPACE_SECTIONS } from './sections';

const ICONS = {
  info: <IconFileText size={16} stroke={1.5} />,
  llm: <IconRobot size={16} stroke={1.5} />,
  memory: <IconBrain size={16} stroke={1.5} />,
  hr: <IconUserStar size={16} stroke={1.5} />,
  tools: <IconSettings size={16} stroke={1.5} />,
  skills: <IconSettings size={16} stroke={1.5} />,
  quotas: <IconChecklist size={16} stroke={1.5} />,
  users: <IconUsers size={16} stroke={1.5} />,
  org: <IconUsers size={16} stroke={1.5} />,
  approvals: <IconShieldCheck size={16} stroke={1.5} />,
  audit: <IconFileText size={16} stroke={1.5} />,
  invites: <IconChecklist size={16} stroke={1.5} />,
} as const;

export default function WorkspaceLayout() {
  return (
    <SurfaceLayout
      headingKey="nav.enterprise"
      headingFallback="Company Settings"
      navItems={WORKSPACE_SECTIONS.map((section) => ({
        to: section.path,
        labelKey: section.labelKey,
        fallbackLabel: section.fallbackLabel,
        icon: ICONS[section.tab],
      }))}
    />
  );
}
