/**
 * Ask-User-Question card — the user-facing clarification surface (CC AskUserQuestion).
 *
 * Rendered inline in chat by `StructuredToolResultBody` whenever a tool returns
 * `status: "awaiting_user_clarification"`. The agent (Plan Mode or normal chat)
 * has ended its turn and is blocked waiting on the user's answer.
 *
 * Each question offers its declared options (single- or multi-select per
 * `multiSelect`) plus an ALWAYS-present "Other" free-text input (CC behaviour —
 * "Other" is never one of the declared options). Submit is enabled once every
 * blocking question has an answer; on submit the formatted multi-line answer is
 * handed to `onSubmit`, which the chat wires to a real outgoing user message so
 * the agent's turn resumes.
 */

import React from 'react';
import { useTranslation } from 'react-i18next';

import type { ClarificationQuestion } from './toolResultEnvelope';

interface AskUserQuestionCardProps {
  questions: ClarificationQuestion[];
  blocking: boolean;
  nextAction?: string | null;
  /** Fired with the formatted multi-line answer when the user submits. */
  onSubmit: (answerText: string) => void | Promise<unknown>;
  /** Compact spacing for inline chat rendering. */
  dense?: boolean;
  /** Marks the card as already answered (post-submit, disabled state). */
  submitted?: boolean;
}

export interface QuestionAnswerState {
  /** Indexes of selected declared options. */
  selected: number[];
  /** Free-text entered into the "Other" field. */
  other: string;
}

export function makeInitialState(questions: ClarificationQuestion[]): QuestionAnswerState[] {
  return questions.map(() => ({ selected: [], other: '' }));
}

function questionLabel(question: ClarificationQuestion): string {
  return question.header.trim() || question.question.trim();
}

function answeredLabels(question: ClarificationQuestion, state: QuestionAnswerState): string[] {
  const labels = state.selected
    .filter((index) => index >= 0 && index < question.options.length)
    .map((index) => question.options[index].label);
  const other = state.other.trim();
  if (other) {
    labels.push(other);
  }
  return labels;
}

export function isQuestionAnswered(question: ClarificationQuestion, state: QuestionAnswerState): boolean {
  return answeredLabels(question, state).length > 0;
}

/**
 * Toggle a declared option for one question. Multi-select adds/removes from the
 * set; single-select replaces the selection and clears any "Other" text.
 * Pure — returns the next state for that question.
 */
export function toggleOptionState(
  state: QuestionAnswerState,
  optionIndex: number,
  multiSelect: boolean,
): QuestionAnswerState {
  if (multiSelect) {
    const exists = state.selected.includes(optionIndex);
    return {
      ...state,
      selected: exists
        ? state.selected.filter((value) => value !== optionIndex)
        : [...state.selected, optionIndex],
    };
  }
  return { selected: [optionIndex], other: '' };
}

/**
 * Set the "Other" free-text for one question. For single-select, non-empty
 * "Other" text replaces any selected declared option (mutually exclusive).
 * Pure — returns the next state for that question.
 */
export function setOtherState(
  state: QuestionAnswerState,
  value: string,
  multiSelect: boolean,
): QuestionAnswerState {
  if (!multiSelect && value.trim()) {
    return { selected: [], other: value };
  }
  return { ...state, other: value };
}

/**
 * Whether the card can be submitted. Blocking cards require every question to
 * be answered; non-blocking cards require at least one.
 */
export function canSubmitClarification(
  questions: ClarificationQuestion[],
  states: QuestionAnswerState[],
  blocking: boolean,
): boolean {
  const answered = (index: number) =>
    isQuestionAnswered(questions[index], states[index] ?? { selected: [], other: '' });
  return blocking ? questions.every((_, index) => answered(index)) : questions.some((_, index) => answered(index));
}

/** One line per question: `{header or question}: {selected labels / Other text}`. */
export function formatClarificationAnswer(
  questions: ClarificationQuestion[],
  states: QuestionAnswerState[],
): string {
  return questions
    .map((question, index) => {
      const labels = answeredLabels(question, states[index] ?? { selected: [], other: '' });
      if (labels.length === 0) {
        return null;
      }
      return `${questionLabel(question)}: ${labels.join(', ')}`;
    })
    .filter((line): line is string => line !== null)
    .join('\n');
}

export default function AskUserQuestionCard({
  questions,
  blocking,
  nextAction,
  onSubmit,
  dense = false,
  submitted = false,
}: AskUserQuestionCardProps) {
  const { t } = useTranslation();
  const [answers, setAnswers] = React.useState<QuestionAnswerState[]>(() => makeInitialState(questions));
  const [busy, setBusy] = React.useState(false);
  const [done, setDone] = React.useState(submitted);

  const toggleOption = (questionIndex: number, optionIndex: number, multiSelect: boolean) => {
    setAnswers((prev) =>
      prev.map((state, index) => (index === questionIndex ? toggleOptionState(state, optionIndex, multiSelect) : state)),
    );
  };

  const setOther = (questionIndex: number, value: string, multiSelect: boolean) => {
    setAnswers((prev) =>
      prev.map((state, index) => (index === questionIndex ? setOtherState(state, value, multiSelect) : state)),
    );
  };

  // Blocking cards require every question answered; the helper is pure so the
  // gating logic is unit-tested directly.
  const canSubmit = !done && !busy && canSubmitClarification(questions, answers, blocking);

  const handleSubmit = async () => {
    if (!canSubmit) return;
    const answerText = formatClarificationAnswer(questions, answers);
    if (!answerText.trim()) return;
    setBusy(true);
    try {
      await onSubmit(answerText);
      setDone(true);
    } finally {
      setBusy(false);
    }
  };

  const labelStyle: React.CSSProperties = {
    fontSize: '11px',
    fontWeight: 700,
    color: 'var(--text-tertiary)',
    textTransform: 'uppercase',
    letterSpacing: '0.4px',
  };

  return (
    <div
      data-testid="ask-user-question-card"
      style={{
        border: '1px solid var(--border-subtle)',
        borderRadius: '10px',
        padding: dense ? '12px 14px' : '16px 18px',
        background: 'var(--bg-primary)',
        display: 'grid',
        gap: dense ? '12px' : '14px',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
        <span
          style={{
            fontSize: '10px',
            fontWeight: 700,
            padding: '2px 8px',
            borderRadius: '999px',
            background: 'var(--bg-secondary)',
            border: '1px solid var(--border-subtle)',
            color: 'var(--accent-text, var(--accent-primary))',
            textTransform: 'uppercase',
            letterSpacing: '0.4px',
          }}
        >
          {t('agent.clarification.badge', 'Needs your input')}
        </span>
      </div>

      {questions.map((question, questionIndex) => {
        const state = answers[questionIndex];
        const otherFieldId = `clarify-${questionIndex}-other`;
        return (
          <div key={questionIndex} style={{ display: 'grid', gap: '8px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
              {question.header && (
                <span
                  style={{
                    fontSize: '10px',
                    fontWeight: 700,
                    padding: '2px 8px',
                    borderRadius: '6px',
                    background: 'var(--bg-secondary)',
                    border: '1px solid var(--border-subtle)',
                    color: 'var(--text-secondary)',
                  }}
                >
                  {question.header}
                </span>
              )}
              {question.multiSelect && (
                <span style={{ ...labelStyle }}>{t('agent.clarification.multiSelect', 'Select all that apply')}</span>
              )}
            </div>
            <div style={{ fontSize: dense ? '13px' : '14px', fontWeight: 600, color: 'var(--text-primary)', lineHeight: 1.5 }}>
              {question.question}
            </div>

            <div style={{ display: 'grid', gap: '6px' }}>
              {question.options.map((option, optionIndex) => {
                const checked = state.selected.includes(optionIndex);
                const inputName = question.multiSelect
                  ? `clarify-${questionIndex}-${optionIndex}`
                  : `clarify-${questionIndex}`;
                return (
                  <label
                    key={optionIndex}
                    style={{
                      display: 'flex',
                      gap: '8px',
                      alignItems: 'flex-start',
                      padding: '8px 10px',
                      borderRadius: '8px',
                      border: `1px solid ${checked ? 'var(--accent-primary)' : 'var(--border-subtle)'}`,
                      background: checked ? 'rgba(16,185,129,0.08)' : 'var(--bg-secondary)',
                      cursor: done ? 'default' : 'pointer',
                    }}
                  >
                    <input
                      type={question.multiSelect ? 'checkbox' : 'radio'}
                      name={inputName}
                      checked={checked}
                      disabled={done || busy}
                      onChange={() => toggleOption(questionIndex, optionIndex, question.multiSelect)}
                      style={{ marginTop: '2px', flexShrink: 0 }}
                    />
                    <span style={{ display: 'grid', gap: '2px', minWidth: 0 }}>
                      <span style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)' }}>{option.label}</span>
                      {option.description && (
                        <span style={{ fontSize: '12px', color: 'var(--text-secondary)', lineHeight: 1.5 }}>
                          {option.description}
                        </span>
                      )}
                    </span>
                  </label>
                );
              })}

              {/* "Other" free-text is ALWAYS offered (CC behaviour). */}
              <div style={{ display: 'grid', gap: '4px' }}>
                <label htmlFor={otherFieldId} style={{ ...labelStyle }}>
                  {t('agent.clarification.other', 'Other')}
                </label>
                <input
                  id={otherFieldId}
                  type="text"
                  value={state.other}
                  disabled={done || busy}
                  placeholder={t('agent.clarification.otherPlaceholder', 'Type your own answer…')}
                  onChange={(event) => setOther(questionIndex, event.target.value, question.multiSelect)}
                  style={{
                    width: '100%',
                    padding: '8px 10px',
                    fontSize: '13px',
                    borderRadius: '8px',
                    border: '1px solid var(--border-subtle)',
                    background: 'var(--bg-secondary)',
                    color: 'var(--text-primary)',
                  }}
                />
              </div>
            </div>
          </div>
        );
      })}

      {nextAction && !done && (
        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', lineHeight: 1.5 }}>{nextAction}</div>
      )}

      {done ? (
        <div
          role="status"
          style={{
            fontSize: '12px',
            color: 'var(--text-secondary)',
            background: 'var(--bg-secondary)',
            border: '1px solid var(--border-subtle)',
            borderRadius: '6px',
            padding: '8px 10px',
          }}
        >
          {t('agent.clarification.sent', 'Your answer was sent.')}
        </div>
      ) : (
        <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
          <button
            type="button"
            className="btn btn-primary"
            style={{ fontSize: '12px', padding: '6px 14px' }}
            disabled={!canSubmit}
            onClick={handleSubmit}
          >
            {busy ? t('common.loading', 'Loading...') : t('agent.clarification.submit', 'Submit answer')}
          </button>
        </div>
      )}
    </div>
  );
}
