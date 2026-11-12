export type RuntimeFailurePresentation = {
  kind: 'model_missing' | 'quota_exhausted' | 'rate_limited' | 'auth_or_permission' | 'retryable' | 'unavailable' | 'delivery_unknown' | 'persistence_error';
  fallback: string;
};

export function runtimeFailurePresentation(
  failureCode: unknown,
  retryable: boolean,
  deliveryState?: unknown,
  terminalReason?: unknown,
): RuntimeFailurePresentation {
  if (terminalReason === 'persistence_error') {
    return {
      kind: 'persistence_error',
      fallback: 'This task did not finish. Please try again later.',
    };
  }
  if (failureCode === 'timeout' && deliveryState === 'unknown') {
    // R18a: a timed-out provider stream that still ends as a visible failure is
    // a plain provider failure — the runtime only fails an unknown delivery
    // when the request held no provider-side effect (genuinely ambiguous sends
    // are fenced as needs_reconciliation and never render this card). It gets
    // the same administrator-directed sentence as other provider failures.
    return {
      kind: 'unavailable',
      fallback: 'The model service could not finish this task. Ask an administrator to check the model configuration.',
    };
  }
  if (deliveryState === 'unknown') {
    // Unknown delivery with retryable=false must not promise a retry: keep
    // only the "not confirmed finished" meaning in plain words.
    return {
      kind: 'delivery_unknown',
      fallback: 'This request did not confirm it finished.',
    };
  }
  if (failureCode === 'auth_or_permission') {
    return {
      kind: 'auth_or_permission',
      fallback: 'Model authentication or permission checks failed. Ask an administrator to check the API key and model access, then retry after fixing the configuration.',
    };
  }
  if (failureCode === 'llm_model_missing') {
    return {
      kind: 'model_missing',
      fallback: 'No model is configured for this Agent. Choose one in Permissions & Settings, or ask an administrator to configure it before retrying.',
    };
  }
  if (failureCode === 'quota_exhausted') {
    return {
      kind: 'quota_exhausted',
      fallback: 'Model quota or balance is insufficient. Ask an administrator to check quota, or switch models and retry.',
    };
  }
  if (failureCode === 'rate_limited') {
    return {
      kind: 'rate_limited',
      fallback: 'The model service is busy. Try again later, or switch models and retry.',
    };
  }
  if (retryable) {
    return {
      kind: 'retryable',
      fallback: 'The model service is temporarily unavailable. This task can be retried safely.',
    };
  }
  return {
    kind: 'unavailable',
    fallback: 'The model service could not finish this task. Ask an administrator to check the model configuration.',
  };
}
