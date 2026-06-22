import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import AskUserQuestionCard, {
  canSubmitClarification,
  formatClarificationAnswer,
  isQuestionAnswered,
  makeInitialState,
  setOtherState,
  toggleOptionState,
  type QuestionAnswerState,
} from './AskUserQuestionCard';
import type { ClarificationQuestion } from './toolResultEnvelope';

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

const TWO_QUESTIONS: ClarificationQuestion[] = [
  {
    question: 'Which asset tracks should the RWA report focus on?',
    header: 'Tracks',
    options: [
      { label: 'US Treasuries', description: 'Ondo/Backed/Mountain' },
      { label: 'Pre-IPO equity', description: 'Securitize/xStocks' },
    ],
    multiSelect: true,
  },
  {
    question: 'How often should it run?',
    header: 'Cadence',
    options: [
      { label: 'Weekly', description: 'Every Monday' },
      { label: 'Monthly', description: 'First of the month' },
    ],
    multiSelect: false,
  },
];

describe('toggleOptionState', () => {
  it('adds and removes options for a multi-select question', () => {
    let state: QuestionAnswerState = { selected: [], other: '' };
    state = toggleOptionState(state, 0, true);
    state = toggleOptionState(state, 1, true);
    expect(state.selected).toEqual([0, 1]);

    state = toggleOptionState(state, 0, true);
    expect(state.selected).toEqual([1]);
  });

  it('replaces selection and clears Other for a single-select question', () => {
    let state: QuestionAnswerState = { selected: [0], other: 'leftover' };
    state = toggleOptionState(state, 1, false);
    expect(state).toEqual({ selected: [1], other: '' });
  });
});

describe('setOtherState', () => {
  it('keeps declared selections alongside Other for multi-select', () => {
    const next = setOtherState({ selected: [0], other: '' }, 'Stablecoins', true);
    expect(next).toEqual({ selected: [0], other: 'Stablecoins' });
  });

  it('clears declared selection when Other is typed for single-select', () => {
    const next = setOtherState({ selected: [1], other: '' }, 'Quarterly', false);
    expect(next).toEqual({ selected: [], other: 'Quarterly' });
  });

  it('does not wipe a single-select option when Other is cleared to blank', () => {
    const next = setOtherState({ selected: [1], other: 'x' }, '   ', false);
    expect(next).toEqual({ selected: [1], other: '   ' });
  });
});

describe('isQuestionAnswered', () => {
  it('is true when an option is selected', () => {
    expect(isQuestionAnswered(TWO_QUESTIONS[0], { selected: [0], other: '' })).toBe(true);
  });

  it('is true when only non-empty Other text is present', () => {
    expect(isQuestionAnswered(TWO_QUESTIONS[0], { selected: [], other: 'Custom' })).toBe(true);
  });

  it('is false when nothing is selected and Other is blank', () => {
    expect(isQuestionAnswered(TWO_QUESTIONS[0], { selected: [], other: '   ' })).toBe(false);
  });
});

describe('canSubmitClarification', () => {
  it('requires every question answered when blocking', () => {
    const partial: QuestionAnswerState[] = [{ selected: [0], other: '' }, { selected: [], other: '' }];
    expect(canSubmitClarification(TWO_QUESTIONS, partial, true)).toBe(false);

    const full: QuestionAnswerState[] = [{ selected: [0], other: '' }, { selected: [1], other: '' }];
    expect(canSubmitClarification(TWO_QUESTIONS, full, true)).toBe(true);
  });

  it('requires only one answer when non-blocking', () => {
    const partial: QuestionAnswerState[] = [{ selected: [0], other: '' }, { selected: [], other: '' }];
    expect(canSubmitClarification(TWO_QUESTIONS, partial, false)).toBe(true);

    const none = makeInitialState(TWO_QUESTIONS);
    expect(canSubmitClarification(TWO_QUESTIONS, none, false)).toBe(false);
  });
});

describe('formatClarificationAnswer', () => {
  it('formats one line per answered question using header + joined labels', () => {
    const states: QuestionAnswerState[] = [
      { selected: [0, 1], other: '' },
      { selected: [0], other: '' },
    ];
    expect(formatClarificationAnswer(TWO_QUESTIONS, states)).toBe(
      'Tracks: US Treasuries, Pre-IPO equity\nCadence: Weekly',
    );
  });

  it('includes Other free-text after declared labels', () => {
    const states: QuestionAnswerState[] = [
      { selected: [0], other: 'Stablecoins' },
      { selected: [], other: 'Daily' },
    ];
    expect(formatClarificationAnswer(TWO_QUESTIONS, states)).toBe(
      'Tracks: US Treasuries, Stablecoins\nCadence: Daily',
    );
  });

  it('falls back to the question text when the header is empty', () => {
    const noHeader: ClarificationQuestion[] = [
      { question: 'What is the deadline?', header: '', options: [], multiSelect: false },
    ];
    expect(formatClarificationAnswer(noHeader, [{ selected: [], other: 'Next Friday' }])).toBe(
      'What is the deadline?: Next Friday',
    );
  });

  it('omits unanswered questions entirely', () => {
    const states: QuestionAnswerState[] = [
      { selected: [], other: '' },
      { selected: [1], other: '' },
    ];
    expect(formatClarificationAnswer(TWO_QUESTIONS, states)).toBe('Cadence: Monthly');
  });
});

describe('AskUserQuestionCard rendering', () => {
  it('renders a single-question carousel panel instead of expanding every question at once', () => {
    const markup = renderToStaticMarkup(
      <AskUserQuestionCard questions={TWO_QUESTIONS} blocking onSubmit={() => undefined} />,
    );

    expect(markup).toContain('Which asset tracks should the RWA report focus on?');
    expect(markup).not.toContain('How often should it run?');
    expect(markup).toContain('Tracks');
    expect(markup).not.toContain('Cadence');
    expect(markup).toContain('US Treasuries');
    expect(markup).toContain('Ondo/Backed/Mountain');
    expect(markup).toContain('Pre-IPO equity');
    expect(markup).toContain('Question 1 of 2');
    expect(markup).toContain('aria-label="Previous question"');
    expect(markup).toContain('aria-label="Next question"');
    // Only the active question owns an "Other" input.
    expect(markup.match(/id="clarify-\d+-other"/g)?.length).toBe(1);
    // Multi-select question renders checkboxes; single-select renders radios.
    expect(markup).toContain('type="checkbox"');
    expect(markup).not.toContain('type="radio"');
  });

  it('disables Submit while the blocking card is unanswered', () => {
    const markup = renderToStaticMarkup(
      <AskUserQuestionCard questions={TWO_QUESTIONS} blocking onSubmit={() => undefined} />,
    );
    // Initial render has no answers → submit button is disabled.
    expect(markup).toContain('Submit answer');
    expect(markup).toMatch(/<button[^>]*disabled[^>]*>[^<]*Submit answer/);
  });

  it('renders the sent confirmation and no submit button when submitted', () => {
    const markup = renderToStaticMarkup(
      <AskUserQuestionCard questions={TWO_QUESTIONS} blocking submitted onSubmit={() => undefined} />,
    );
    expect(markup).toContain('Your answer was sent.');
    expect(markup).not.toContain('Submit answer');
  });
});
