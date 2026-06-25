export type ComposerSlashShortcut = 'goal' | 'schedule';

export function composerShortcutText(shortcut: ComposerSlashShortcut): string {
  if (shortcut === 'goal') return '/goal ';
  return '/schedule ';
}
