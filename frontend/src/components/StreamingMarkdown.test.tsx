import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

// Newline-gated streaming contract (Codex markdown_stream parity):
// - streaming: markdown renders only the committed prefix (up to the last
//   newline); the tail stays plain text with a soft cursor
// - not streaming: the whole content renders as markdown

vi.mock('./MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}));

import StreamingMarkdown from './StreamingMarkdown';

describe('StreamingMarkdown', () => {
  it('renders only the committed prefix as markdown while streaming', () => {
    const html = renderToStaticMarkup(
      <StreamingMarkdown content={'# Title\nfirst line\npartial tai'} streaming />,
    );
    expect(html).toContain('# Title\nfirst line\n');
    expect(html).toContain('session-tui-stream-tail');
    expect(html).toContain('partial tai');
    // the tail must NOT be inside the markdown renderer
    expect(html).not.toContain('partial tai</div>');
  });

  it('renders the full content as markdown once streaming ends', () => {
    const html = renderToStaticMarkup(
      <StreamingMarkdown content={'# Title\nfull answer'} streaming={false} />,
    );
    expect(html).toContain('# Title\nfull answer');
    expect(html).not.toContain('session-tui-stream-tail');
  });

  it('shows a bare cursor when streaming with no content yet', () => {
    const html = renderToStaticMarkup(<StreamingMarkdown content="" streaming />);
    expect(html).toContain('session-tui-stream-tail');
  });
});
