import React from 'react';

import StreamingMarkdown from '../../components/StreamingMarkdown';
import type { AgentChatMessage } from './chatRuntime';

export function shouldCollapseAssistantSupplement(messages: AgentChatMessage[], index: number): boolean {
  const message = messages[index];
  if (message?.role !== 'assistant' || !message.content.trim()) return false;

  let start = index - 1;
  let end = index + 1;
  while (start >= 0 && messages[start].role !== 'user') start -= 1;
  while (end < messages.length && messages[end].role !== 'user') end += 1;
  return messages.slice(start + 1, end).some(
    (candidate) => candidate.role === 'tool_call' && candidate.toolMeta && candidate.toolMeta.kind === 'hr_preview',
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
