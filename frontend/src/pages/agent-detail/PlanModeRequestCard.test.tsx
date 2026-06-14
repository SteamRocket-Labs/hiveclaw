import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import PlanModeRequestCard, { makeDecisionHandlers } from './PlanModeRequestCard';

// Mirror the lightweight i18n mock used across AgentDetail section tests: the
// second argument is the English fallback, with {{var}} interpolation.
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallbackOrOptions?: string | Record<string, unknown>, options?: Record<string, unknown>) => {
      if (typeof fallbackOrOptions === 'string') {
        return fallbackOrOptions.replace(/\{\{\s*(\w+)\s*\}\}/g, (_, name) => String(options?.[name] ?? ''));
      }
      return key.split('.').pop() ?? key;
    },
  }),
}));

const REASON = 'This spans several files and an external send — confirm the plan first.';

describe('PlanModeRequestCard rendering', () => {
  it('renders the agent name, the reason, and both approve / decline buttons', () => {
    const markup = renderToStaticMarkup(
      <PlanModeRequestCard agentName="Research Bot" reason={REASON} onApprove={() => undefined} onDecline={() => undefined} />,
    );

    expect(markup).toContain('Research Bot');
    expect(markup).toContain(REASON);
    expect(markup).toContain('Approve and enter Plan Mode');
    expect(markup).toContain('Not needed');
  });

  it('still renders the reason and actions when no agent name is given', () => {
    const markup = renderToStaticMarkup(
      <PlanModeRequestCard reason={REASON} onApprove={() => undefined} onDecline={() => undefined} />,
    );
    expect(markup).toContain(REASON);
    expect(markup).toContain('Approve and enter Plan Mode');
  });

  it('renders the sent confirmation and no action buttons when submitted', () => {
    const markup = renderToStaticMarkup(
      <PlanModeRequestCard reason={REASON} onApprove={() => undefined} onDecline={() => undefined} submitted />,
    );
    expect(markup).toContain('Your decision was sent.');
    expect(markup).not.toContain('Approve and enter Plan Mode');
    expect(markup).not.toContain('Not needed');
  });
});

// The codebase has no DOM test environment (no jsdom / testing-library), so the
// click→callback wiring is verified through the same pure-helper pattern that
// AskUserQuestionCard uses for its interaction logic: the component delegates its
// approve/decline handlers to makeDecisionHandlers, tested directly here.
describe('makeDecisionHandlers', () => {
  it('routes approve and decline to the right callbacks', () => {
    const onApprove = vi.fn();
    const onDecline = vi.fn();
    let decided = false;
    const setDecided = (value: boolean) => {
      decided = value;
    };

    const handlers = makeDecisionHandlers({ onApprove, onDecline, decided, setDecided });
    handlers.approve();
    expect(onApprove).toHaveBeenCalledTimes(1);
    expect(onDecline).not.toHaveBeenCalled();
    expect(decided).toBe(true);
  });

  it('routes decline without firing approve', () => {
    const onApprove = vi.fn();
    const onDecline = vi.fn();
    let decided = false;
    const handlers = makeDecisionHandlers({
      onApprove,
      onDecline,
      decided,
      setDecided: (value) => {
        decided = value;
      },
    });
    handlers.decline();
    expect(onDecline).toHaveBeenCalledTimes(1);
    expect(onApprove).not.toHaveBeenCalled();
    expect(decided).toBe(true);
  });

  it('is a single decision — once decided, neither callback fires again', () => {
    const onApprove = vi.fn();
    const onDecline = vi.fn();
    // Already decided → handlers must be no-ops (the card disables the buttons,
    // and this guard backstops a double-fire).
    const handlers = makeDecisionHandlers({
      onApprove,
      onDecline,
      decided: true,
      setDecided: () => undefined,
    });
    handlers.approve();
    handlers.decline();
    expect(onApprove).not.toHaveBeenCalled();
    expect(onDecline).not.toHaveBeenCalled();
  });
});
