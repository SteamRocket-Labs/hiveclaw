import React from 'react';

import StreamingMarkdown from '../../components/StreamingMarkdown';
import type { AgentChatMessage } from './chatRuntime';

export function shouldCollapseAssistantSupplement(messages: AgentChatMessage[], index: number): boolean {
  const message = messages[index];
  if (message?.role !== 'assistant' || !String(message.content || '').trim()) return false;

  for (let cursor = index - 1; cursor >= 0; cursor -= 1) {
    const candidate = messages[cursor];
    if (candidate?.role === 'user') return false;
    if (candidate?.role === 'tool_call' && candidate.toolMeta?.kind === 'hr_preview') return true;
  }
  return false;
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
