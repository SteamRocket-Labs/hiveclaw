import React from 'react';

// Codex ReasoningSummaryCell parity: the thinking HEADLINE is visible inline
// as a dim italic line (`• planned the fix…`) — the summary is the default,
// the full reasoning expands on demand. This inverts the old pattern (full
// text folded behind a bare "Thinking" header with zero information).

export function extractThinkingHeadline(thinking: string): string {
  for (const raw of (thinking || '').split('\n')) {
    const line = raw.replace(/^[#>*\-•\s]+/, '').trim();
    if (line) return line.length > 140 ? `${line.slice(0, 139)}…` : line;
  }
  return '';
}

type ThinkingDisclosureProps = {
  thinking: string;
  streaming?: boolean;
};

export default function ThinkingDisclosure({ thinking, streaming = false }: ThinkingDisclosureProps) {
  const [expanded, setExpanded] = React.useState(false);
  const text = (thinking || '').trim();
  if (!text) return null;
  // While the model is still thinking, surface the newest line so the
  // headline tracks the live reasoning; once done it settles on the first.
  const headline = streaming
    ? extractThinkingHeadline(text.split('\n').filter((line) => line.trim()).slice(-1).join('')) ||
      extractThinkingHeadline(text)
    : extractThinkingHeadline(text);
  return (
    <div data-testid="thinking-disclosure">
      <button
        type="button"
        className="session-tui-thinking-headline"
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
        title={expanded ? undefined : headline}
      >
        <span className="session-tui-thinking-bullet">•</span>
        <span className={`session-tui-thinking-text${streaming ? ' session-tui-shimmer' : ''}`}>
          {headline}
        </span>
      </button>
      {expanded && <div className="session-tui-thinking-full">{text}</div>}
    </div>
  );
}
