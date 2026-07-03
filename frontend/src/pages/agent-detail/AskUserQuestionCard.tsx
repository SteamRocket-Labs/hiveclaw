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
import { IconChevronLeft, IconChevronRight } from '@tabler/icons-react';
import { useTranslation } from 'react-i18next';

import './AskUserQuestionCard.css';
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
  onSubmit,
  dense = false,
  submitted = false,
}: AskUserQuestionCardProps) {
  const { t } = useTranslation();
  const questionsFingerprint = React.useMemo(
    () =>
      JSON.stringify(
        questions.map((question) => ({
          question: question.question,
          header: question.header,
          multiSelect: question.multiSelect,
          options: question.options.map((option) => [option.label, option.description ?? '']),
        })),
      ),
    [questions],
  );
  const [answers, setAnswers] = React.useState<QuestionAnswerState[]>(() => makeInitialState(questions));
  const [activeQuestionIndex, setActiveQuestionIndex] = React.useState(0);
  const [busy, setBusy] = React.useState(false);
  const [done, setDone] = React.useState(submitted);

  React.useEffect(() => {
    setAnswers(makeInitialState(questions));
    setActiveQuestionIndex(0);
    setDone(submitted);
  }, [questionsFingerprint, submitted]);

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
  const questionCount = questions.length;
  const safeActiveQuestionIndex = Math.min(activeQuestionIndex, Math.max(questionCount - 1, 0));
  const activeQuestion = questions[safeActiveQuestionIndex];
  const activeState = answers[safeActiveQuestionIndex] ?? { selected: [], other: '' };
  const isFirstQuestion = safeActiveQuestionIndex === 0;
  const isLastQuestion = safeActiveQuestionIndex >= questionCount - 1;
  const answeredCount = questions.filter((question, index) =>
    isQuestionAnswered(question, answers[index] ?? { selected: [], other: '' }),
  ).length;

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

  if (questionCount === 0 || !activeQuestion) {
    return null;
  }

  const otherFieldId = `clarify-${safeActiveQuestionIndex}-other`;
  const progressText = t('agent.clarification.progress', 'Question {{current}} of {{total}}', {
    current: safeActiveQuestionIndex + 1,
    total: questionCount,
  });

  return (
    <div
      data-testid="ask-user-question-card"
      className={`ask-question-card${dense ? ' is-dense' : ''}`}
    >
      <div className="ask-question-topbar">
        <span className="ask-question-badge">
          {t('agent.clarification.badge', 'Needs your input')}
        </span>
        <span className="ask-question-progress">{progressText}</span>
      </div>

      <div className="ask-question-body">
        <div className="ask-question-header-row">
          {activeQuestion.header && (
            <span className="ask-question-header-chip">
              {activeQuestion.header}
            </span>
          )}
          {activeQuestion.multiSelect && (
            <span className="ask-question-label">{t('agent.clarification.multiSelect', 'Select all that apply')}</span>
          )}
        </div>
        <div className="ask-question-question">
          {activeQuestion.question}
        </div>

        <div className="ask-question-options">
          {activeQuestion.options.map((option, optionIndex) => {
            const checked = activeState.selected.includes(optionIndex);
            const inputName = activeQuestion.multiSelect
              ? `clarify-${safeActiveQuestionIndex}-${optionIndex}`
              : `clarify-${safeActiveQuestionIndex}`;
            return (
              <label
                key={optionIndex}
                className={`ask-question-option${checked ? ' is-checked' : ''}${done ? ' is-locked' : ''}`}
              >
                <input
                  type={activeQuestion.multiSelect ? 'checkbox' : 'radio'}
                  name={inputName}
                  checked={checked}
                  disabled={done || busy}
                  onChange={() => toggleOption(safeActiveQuestionIndex, optionIndex, activeQuestion.multiSelect)}
                  className="ask-question-option-input"
                />
                <span className="ask-question-option-text">
                  <span className="ask-question-option-label">{option.label}</span>
                  {option.description && (
                    <span className="ask-question-option-desc">
                      {option.description}
                    </span>
                  )}
                </span>
              </label>
            );
          })}

          {/* "Other" free-text is ALWAYS offered (CC behaviour). */}
          <div className="ask-question-other">
            <label htmlFor={otherFieldId} className="ask-question-label">
              {t('agent.clarification.other', 'Other')}
            </label>
            <input
              id={otherFieldId}
              type="text"
              value={activeState.other}
              disabled={done || busy}
              placeholder={t('agent.clarification.otherPlaceholder', 'Type your own answer…')}
              onChange={(event) => setOther(safeActiveQuestionIndex, event.target.value, activeQuestion.multiSelect)}
              className="ask-question-input"
            />
          </div>
        </div>
      </div>

      <div className="ask-question-dots" aria-hidden="true">
        {questions.map((question, index) => {
          const isActive = index === safeActiveQuestionIndex;
          const isAnswered = isQuestionAnswered(question, answers[index] ?? { selected: [], other: '' });
          return (
            <span key={index} className="ask-question-dot-slot">
              <span
                className={`ask-question-dot${isAnswered ? ' is-answered' : ''}${isActive ? ' is-active' : ''}`}
              />
            </span>
          );
        })}
      </div>

      {done ? (
        <div role="status" className="ask-question-sent">
          {t('agent.clarification.sent', 'Your answer was sent.')}
        </div>
      ) : (
        <div className="ask-question-footer">
          <div className="ask-question-nav">
            <button
              type="button"
              aria-label={t('agent.clarification.previousQuestion', 'Previous question')}
              title={t('agent.clarification.previousQuestion', 'Previous question')}
              disabled={busy || isFirstQuestion}
              onClick={() => setActiveQuestionIndex((index) => Math.max(0, index - 1))}
              className="ask-question-nav-btn"
            >
              <IconChevronLeft size={16} stroke={2.3} />
            </button>
            <button
              type="button"
              aria-label={t('agent.clarification.nextQuestion', 'Next question')}
              title={t('agent.clarification.nextQuestion', 'Next question')}
              disabled={busy || isLastQuestion}
              onClick={() => setActiveQuestionIndex((index) => Math.min(questionCount - 1, index + 1))}
              className="ask-question-nav-btn"
            >
              <IconChevronRight size={16} stroke={2.3} />
            </button>
          </div>
          <span className="ask-question-count">
            {t('agent.clarification.answeredProgress', '{{answered}}/{{total}} answered', {
              answered: answeredCount,
              total: questionCount,
            })}
          </span>
          <button
            type="button"
            className="btn btn-primary"
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
