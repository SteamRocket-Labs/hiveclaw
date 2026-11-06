import type { TFunction } from 'i18next';
import { approvalActionFor } from '../session-workbench/approvalActionPresentation';
import { titleForToolMessage } from './chatDisclosureReducer';

const TOOL_TITLE_KEY: Record<string, string> = {
  Thinking: 'agent.chat.phase.thinking',
  Working: 'agent.chat.disclosure.working',
  'Waiting for input': 'agent.chat.disclosure.waiting',
  'Delegation update': 'agent.chat.disclosure.toolTitle.delegationStep',
};

/** Share the conversation's tool words with handover notices. */
export function toolStepLabel(toolName: string | null, t: TFunction, sentinel?: string): string {
  const action = approvalActionFor(toolName);
  if (action) return t(action.key, action.fallback);
  const title = sentinel ?? titleForToolMessage({ toolName: toolName ?? undefined });
  const key = TOOL_TITLE_KEY[title] ?? `agent.chat.disclosure.toolTitle.${title
    .split(/[^a-zA-Z0-9]+/)
    .filter(Boolean)
    .map((part, index) => (index ? part.charAt(0).toUpperCase() + part.slice(1) : part.toLowerCase()))
    .join('')}`;
  return t(key, title);
}
