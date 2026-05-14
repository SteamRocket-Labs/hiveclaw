import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import DeepResearchStreamPanel from './DeepResearchStreamPanel';

describe('DeepResearchStreamPanel (Tier 3-4 UI)', () => {
  it('renders the initial idle state with stat tiles and a task identifier', () => {
    const markup = renderToStaticMarkup(
      <DeepResearchStreamPanel
        agentId="agent-test"
        taskId="task-abcdef0123456789"
      />,
    );

    // Status badge starts in idle (useEffect that flips to streaming does not run during SSR).
    // Text is "Idle" — uppercasing is CSS-only, not in the rendered DOM.
    expect(markup).toContain('Idle');
    // Truncated task id surfaces in the header
    expect(markup).toContain('task-abc');
    // Five stat tiles wired to the bucket counters
    expect(markup).toContain('Sources');
    expect(markup).toContain('Claims');
    expect(markup).toContain('Lanes');
    expect(markup).toContain('Steps');
    expect(markup).toContain('Heartbeats');
    // Carries the testid hook for downstream integration tests
    expect(markup).toContain('data-testid="deep-research-stream-panel"');
  });

  it('renders zero counts on every stat tile before any event arrives', () => {
    const markup = renderToStaticMarkup(
      <DeepResearchStreamPanel agentId="agent-test" taskId="task-xyz" />,
    );

    // 5 stat tiles × value=0 ⇒ at least 5 occurrences of `>0</` inside the bold value spans.
    const zeroValueOccurrences = (markup.match(/>0</g) || []).length;
    expect(zeroValueOccurrences).toBeGreaterThanOrEqual(5);
  });

  it('does not render the abort button while status is idle (only while streaming)', () => {
    const markup = renderToStaticMarkup(
      <DeepResearchStreamPanel agentId="agent-test" taskId="task-xyz" />,
    );

    expect(markup).not.toContain('Stop stream');
  });

  it('does not render report-preview or error blocks when there is no markdown or error', () => {
    const markup = renderToStaticMarkup(
      <DeepResearchStreamPanel agentId="agent-test" taskId="task-xyz" />,
    );

    expect(markup).not.toContain('Report preview');
    expect(markup).not.toContain('Stream error');
    expect(markup).not.toContain('Latest reflection');
    expect(markup).not.toContain('Controller action');
  });
});
