// @vitest-environment jsdom
import React from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SessionGitLine } from './SessionLineageSurface';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
afterEach(cleanup);

const checkpoints = [
  { checkpoint_event_id: 'cp-1', prompt: 'First step' },
  { checkpoint_event_id: 'cp-2', prompt: 'Second step' },
];

describe('session gitline touch reveal (B5 review)', () => {
  it('marks the rail touch-revealed on the first tap; a second tap acts on the node', () => {
    const onNavigateCheckpoint = vi.fn();
    render(
      <SessionGitLine
        activeSessionId="session-1"
        checkpoints={checkpoints}
        lineage={[]}
        onNavigateCheckpoint={onNavigateCheckpoint}
      />,
    );

    const rail = screen.getByTestId('session-gitline');
    expect(rail.className).not.toContain('is-touch-revealed');

    // First tap lands on the rail itself — hidden nodes are un-hittable
    // (pointer-events: none, asserted in FrontendSurfaceHygiene's CSS
    // contract), so this tap reveals without acting on any checkpoint.
    fireEvent.click(rail);
    expect(rail.className).toContain('is-touch-revealed');

    // A second tap reaches a now-visible checkpoint and still acts.
    fireEvent.click(screen.getAllByTestId('session-gitline-checkpoint')[0]);
    expect(onNavigateCheckpoint).toHaveBeenCalledTimes(1);
  });
});
