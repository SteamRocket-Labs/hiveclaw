import React from 'react';

import MarkdownRenderer from './MarkdownRenderer';

// Newline-gated streaming markdown (Codex markdown_stream.rs parity):
// while streaming, only the prefix up to the last newline is handed to the
// (memoized) MarkdownRenderer — that committed string stays stable across
// token deltas within a line, so already-rendered markdown never re-renders
// or flickers. The uncommitted tail renders as plain text with a soft cursor
// until its newline commits. When streaming ends, the full content renders.

type StreamingMarkdownProps = {
  content: string;
  streaming: boolean;
  style?: React.CSSProperties;
  className?: string;
};

function splitAtLastNewline(content: string): { committed: string; tail: string } {
  const index = content.lastIndexOf('\n');
  if (index < 0) return { committed: '', tail: content };
  return { committed: content.slice(0, index + 1), tail: content.slice(index + 1) };
}

export default function StreamingMarkdown({ content, streaming, style, className }: StreamingMarkdownProps) {
  if (!streaming) {
    return <MarkdownRenderer content={content} style={style} className={className} />;
  }
  const { committed, tail } = splitAtLastNewline(content);
  return (
    <div className={className} style={style} data-testid="streaming-markdown">
      {committed && <MarkdownRenderer content={committed} />}
      {tail && (
        <span className="session-tui-stream-tail" style={{ whiteSpace: 'pre-wrap' }}>
          {tail}
        </span>
      )}
      {!committed && !tail && <span className="session-tui-stream-tail" />}
    </div>
  );
}
