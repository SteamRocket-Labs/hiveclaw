import React from 'react';

import StreamingMarkdown from '../../components/StreamingMarkdown';
import type { AgentChatMessage } from './chatRuntime';

export function isSupersededCreationCard(messages: AgentChatMessage[], index: number): boolean {
  const card = messages[index]?.toolMeta;
  if (card?.kind !== 'hr_preview' || !card.blueprintId) return false;
  return messages.slice(index + 1).some((message) => message.toolMeta?.kind === 'hr_preview'
    && message.toolMeta.blueprintId === card.blueprintId);
}

export function assistantReplyForDisplay(content: string): string {
  const lines = content.match(/[^\n]*\n|[^\n]+$/g) || [];
  const removed = new Set<number>();
  for (let index = 0; index < lines.length; index++) {
    if (!/^[\t ]*(?:[-*][\t ]+)?(?:\*\*|`)?DELIVERABLE(?:\*\*|`)?[\t ]*[:：]/i.test(lines[index])) continue;
    removed.add(index);
    let previous = index - 1;
    while (previous >= 0 && !lines[previous].trim()) previous--;
    if (previous < 0) continue;
    const label = lines[previous].replace(/[*_]/g, '').trim().replace(/^#+\s*/, '').replace(/^(?:-|\d+\.)\s+/, '').trim();
    if (Array.from(label).length <= 30 && /[:：]$/.test(label) && !/[。，,；;！!？?]/.test(label.slice(0, -1))) {
      for (let line = previous; line < index; line++) removed.add(line);
    }
  }
  return lines.filter((_line, index) => !removed.has(index)).join('');
}

export function shouldCollapseAssistantSupplement(messages: AgentChatMessage[], index: number): boolean {
  const message = messages[index];
  if (message?.role !== 'assistant' || !message.content.trim()) return false;

  let start = index - 1;
  let end = index + 1;
  while (start >= 0 && messages[start].role !== 'user') start -= 1;
  while (end < messages.length && messages[end].role !== 'user') end += 1;
  return messages.slice(start + 1, end).some(
    (candidate) => candidate.role === 'tool_call' && candidate.toolName !== 'create_employee_from_request'
      && candidate.toolMeta && candidate.toolMeta.kind === 'hr_preview',
  );
}

export function AssistantMessageBody({
  content,
  streaming,
  supplemental,
  supplementalLabel,
}: {
  content: string;
  streaming: boolean;
  supplemental: boolean;
  supplementalLabel: string;
}) {
  content = assistantReplyForDisplay(content);
  if (!supplemental) return <StreamingMarkdown content={content} streaming={streaming} />;

  return (
    <details className="session-tui-assistant-supplement" data-testid="assistant-canonical-card-supplement">
      <summary>{supplementalLabel}</summary>
      <div className="session-tui-assistant-supplement-body">
        <StreamingMarkdown content={content} streaming={streaming} />
      </div>
    </details>
  );
}
