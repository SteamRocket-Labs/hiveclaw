import { describe, expect, it } from 'vitest';

import {
  buildAssignmentHandoff,
  buildAssignmentSessionTitle,
  readAssignmentHandoff,
} from './assignmentHandoff';

describe('assignment handoff contract', () => {
  it('normalizes the user request and preserves the selected execution intent', () => {
    const handoff = buildAssignmentHandoff('  Prepare the board report.  ', 'goal');

    expect(handoff).toEqual({ content: 'Prepare the board report.', intent: 'goal' });
    expect(readAssignmentHandoff({ assignmentDraft: handoff })).toEqual(handoff);
  });

  it('rejects stale route state and builds a compact session title', () => {
    expect(readAssignmentHandoff({ assignmentDraft: { content: ' ', intent: 'plan' } })).toBeNull();
    expect(readAssignmentHandoff({ assignmentDraft: { content: 'work', intent: 'unknown' } })).toBeNull();
    expect(buildAssignmentSessionTitle('First line for the user\nsecond line')).toBe('First line for the user');
    expect(buildAssignmentSessionTitle('x'.repeat(120))).toHaveLength(80);
  });
});
