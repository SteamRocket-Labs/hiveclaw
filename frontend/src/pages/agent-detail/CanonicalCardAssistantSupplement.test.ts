import { expect, it } from 'vitest';
import { isSupersededCreationCard } from './CanonicalCardAssistantSupplement';
import type { AgentChatMessage } from './chatRuntime';

it('renders one creation card after a retried tool call while retaining separate requests', () => {
  const messages = [
    { role: 'tool_call', toolMeta: { kind: 'hr_preview', authorizationMode: 'policy_direct', blueprintId: 'same-request' } },
    { role: 'tool_call', toolMeta: { kind: 'hr_preview', authorizationMode: 'policy_direct', blueprintId: 'different-request' } },
    { role: 'tool_call', toolMeta: { kind: 'hr_preview', authorizationMode: 'policy_direct', blueprintId: 'same-request' } },
  ] as AgentChatMessage[];
  expect(messages.filter((_, index) => !isSupersededCreationCard(messages, index))).toEqual([messages[1], messages[2]]);
});
