import { expect, it } from 'vitest';
import { isSupersededCreationCard, shouldCollapseAssistantSupplement } from './CanonicalCardAssistantSupplement';
import type { AgentChatMessage } from './chatRuntime';

it('renders one creation card after a retried tool call while retaining separate requests', () => {
  const messages = [
    { role: 'tool_call', toolMeta: { kind: 'hr_preview', authorizationMode: 'policy_direct', blueprintId: 'same-request' } },
    { role: 'tool_call', toolMeta: { kind: 'hr_preview', authorizationMode: 'policy_direct', blueprintId: 'different-request' } },
    { role: 'tool_call', toolMeta: { kind: 'hr_preview', authorizationMode: 'policy_direct', blueprintId: 'same-request' } },
  ] as AgentChatMessage[];
  expect(messages.filter((_, index) => !isSupersededCreationCard(messages, index))).toEqual([messages[1], messages[2]]);
});

it.each(['awaiting_confirmation', 'rejected', 'completed'])('keeps one card when a %s draft is returned again', (status) => {
  const messages = [
    { role: 'tool_call', toolMeta: { kind: 'hr_preview', blueprintId: 'same-request', status } },
    { role: 'tool_call', toolMeta: { kind: 'hr_preview', blueprintId: 'same-request', status } },
    { role: 'tool_call', toolMeta: { kind: 'hr_preview', blueprintId: 'another-request', status } },
  ] as AgentChatMessage[];
  expect(messages.filter((_, index) => !isSupersededCreationCard(messages, index))).toEqual([messages[1], messages[2]]);
});

it('shows the short creation reply in the conversation', () => {
  const messages = [
    { role: 'tool_call', toolName: 'create_employee_from_request', toolMeta: { kind: 'hr_preview' } },
    { role: 'assistant', content: '请在卡片中确认并创建数字员工。' },
  ] as AgentChatMessage[];
  expect(shouldCollapseAssistantSupplement(messages, 1)).toBe(false);
});
