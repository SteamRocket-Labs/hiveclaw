import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import ThinkingDisclosure, { extractThinkingHeadline } from './ThinkingDisclosure';

// Codex ReasoningSummaryCell parity: headline visible by default (dim italic),
// full reasoning behind an expand — never a bare "Thinking" label hiding
// everything.

describe('extractThinkingHeadline', () => {
  it('returns the first meaningful line stripped of markdown prefixes', () => {
    expect(extractThinkingHeadline('\n## Plan the fix\nmore detail')).toBe('Plan the fix');
    expect(extractThinkingHeadline('- consider caching\nrest')).toBe('consider caching');
  });

  it('truncates very long headlines', () => {
    const long = 'x'.repeat(200);
    expect(extractThinkingHeadline(long).length).toBeLessThanOrEqual(140);
    expect(extractThinkingHeadline(long).endsWith('…')).toBe(true);
  });
});

describe('ThinkingDisclosure', () => {
  it('shows the headline inline and hides the full text by default', () => {
    const html = renderToStaticMarkup(
      <ThinkingDisclosure thinking={'Plan the fix first\nthen check the failing test in detail'} />,
    );
    expect(html).toContain('session-tui-thinking-headline');
    expect(html).toContain('Plan the fix first');
    expect(html).toContain('aria-expanded="false"');
    expect(html).not.toContain('session-tui-thinking-full');
    expect(html).not.toContain('then check the failing test');
  });

  it('shimmers the live headline while streaming', () => {
    const html = renderToStaticMarkup(
      <ThinkingDisclosure thinking={'first thought\nlatest live line'} streaming />,
    );
    expect(html).toContain('session-tui-shimmer');
    expect(html).toContain('latest live line');
  });

  it('renders nothing for empty thinking', () => {
    expect(renderToStaticMarkup(<ThinkingDisclosure thinking="   " />)).toBe('');
  });
});
