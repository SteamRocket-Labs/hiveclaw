export function buildPlanModeScopeKey(
  agentId: string | null | undefined,
  sessionId: string | number | null | undefined,
): string {
  return `${agentId ?? ''}:${sessionId ?? ''}`;
}

export function nextPlanModeRequestedForScope({
  currentRequested,
  previousScopeKey,
  nextScopeKey,
}: {
  currentRequested: boolean;
  previousScopeKey: string;
  nextScopeKey: string;
}): boolean {
  return previousScopeKey === nextScopeKey ? currentRequested : false;
}
