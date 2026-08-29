import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import HrCreationHandoffCard, { parseHrCreationHandoffResult } from './HrCreationHandoffCard';


describe('HR creation handoff card', () => {
  it('offers one user-facing continuation action without printing internal ids', () => {
    const markup = renderToStaticMarkup(
      <HrCreationHandoffCard
        rawResult={JSON.stringify({
          ok: true,
          status: 'hr_handoff_started',
          hr_agent_id: 'hr-agent-id',
          hr_session_id: 'hr-session-id',
          source_agent_name: 'Planning Agent',
        })}
      />,
    );

    expect(markup).toContain('href="/agents/hr-agent-id?session_id=hr-session-id#chat"');
    expect(markup).toContain('>Continue with HR Agent</a>');
    expect(markup).toContain('Planning Agent handed this creation request to HR Agent.');
    expect(markup).not.toContain('>hr-agent-id<');
    expect(markup).not.toContain('>hr-session-id<');
  });

  it('accepts the exact queued replay state and rejects malformed receipts', () => {
    expect(parseHrCreationHandoffResult({
      ok: true,
      status: 'hr_handoff_queued',
      hr_agent_id: 'hr-agent-id',
      hr_session_id: 'hr-session-id',
    })).toMatchObject({ hrAgentId: 'hr-agent-id', hrSessionId: 'hr-session-id' });
    expect(parseHrCreationHandoffResult('{"ok":true}')).toBeNull();
  });

  it('shows a user recovery message instead of raw malformed output', () => {
    const markup = renderToStaticMarkup(<HrCreationHandoffCard rawResult="internal-id-123" />);
    expect(markup).toContain('HR Agent handoff could not be opened. Ask the Agent to retry.');
    expect(markup).not.toContain('internal-id-123');
  });
});
