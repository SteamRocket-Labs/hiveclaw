import type { SessionPermissionMode } from './AgentChatSection';
import { defaultSessionPermissionModeFromAgent } from './agentDetailPolicy';

export type ComposerSlashShortcut = 'goal' | 'schedule';

export function composerShortcutText(shortcut: ComposerSlashShortcut): string {
  if (shortcut === 'goal') return '/goal ';
  return '/schedule ';
}

// Composer-chrome presentation policy: what the permission/intent affordances
// say and when they show. The badge is hidden on the agent's default mode
// (P3); the "+" menu keeps permission selection reachable.

const SESSION_PERMISSION_MODE_OPTIONS: Array<{
  value: SessionPermissionMode;
  label: string;
  description: string;
}> = [
  { value: 'default', label: 'Ask first', description: 'Ask before sensitive session actions' },
  { value: 'auto', label: 'Approve for me', description: 'Approve low-risk actions and ask for risky ones' },
  { value: 'bypassPermissions', label: 'Full access', description: 'Bypass session prompts, still obey enterprise rules' },
];

export const sessionPermissionModeOptions = () => SESSION_PERMISSION_MODE_OPTIONS;

export function getSessionPermissionModeLabel(
  mode: SessionPermissionMode,
  t: (key: string, fallback: string) => string,
): string {
  if (mode === 'default') return t('agent.chat.composer.permissionMode.default', 'Ask first');
  if (mode === 'bypassPermissions') return t('agent.chat.composer.permissionMode.bypassPermissions', 'Full access');
  return t('agent.chat.composer.permissionMode.auto', 'Approve for me');
}

export function sessionPermissionModeVisible(
  agent: unknown,
  mode: SessionPermissionMode,
  planModeRequested: boolean,
  goalModeRequested: boolean,
): boolean {
  return mode !== defaultSessionPermissionModeFromAgent(agent) || planModeRequested || goalModeRequested;
}

export function getComposerIntentLabel(
  planModeRequested: boolean,
  goalModeRequested: boolean,
  t: (key: string, fallback: string) => string,
): string | null {
  if (goalModeRequested) return t('agent.chat.composer.goalModeActive', 'Goal mode');
  if (planModeRequested) return t('agent.chat.composer.planModeActive', 'Plan Mode');
  return null;
}
