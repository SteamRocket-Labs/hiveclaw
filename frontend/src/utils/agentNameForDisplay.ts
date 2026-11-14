export function agentNameForDisplay(name: string, t: (key: string, fallback: string) => string): string {
  return name === '__system_hr__' ? t('handover.hrAgentName', 'HR assistant') : name;
}
