export type AssignmentIntent = 'execute' | 'plan' | 'goal';

export interface AssignmentHandoff {
  content: string;
  intent: AssignmentIntent;
}

const ASSIGNMENT_INTENTS = new Set<AssignmentIntent>(['execute', 'plan', 'goal']);

export function buildAssignmentHandoff(content: string, intent: AssignmentIntent): AssignmentHandoff {
  const normalized = content.trim();
  if (!normalized) throw new Error('Assignment content is required.');
  return { content: normalized, intent };
}

export function buildAssignmentSessionTitle(content: string): string {
  const firstLine = content.trim().split(/\r?\n/, 1)[0]?.trim() || 'Assigned work';
  return firstLine.slice(0, 80);
}

export function readAssignmentHandoff(state: unknown): AssignmentHandoff | null {
  if (!state || typeof state !== 'object') return null;
  const draft = (state as { assignmentDraft?: unknown }).assignmentDraft;
  if (!draft || typeof draft !== 'object') return null;
  const content = String((draft as { content?: unknown }).content || '').trim();
  const intent = String((draft as { intent?: unknown }).intent || '') as AssignmentIntent;
  if (!content || !ASSIGNMENT_INTENTS.has(intent)) return null;
  return { content, intent };
}
