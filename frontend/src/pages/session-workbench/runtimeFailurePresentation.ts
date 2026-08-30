export type RuntimeFailurePresentation = {
  kind: 'model_missing' | 'quota_exhausted' | 'rate_limited' | 'retryable' | 'unavailable';
  fallback: string;
};

export function runtimeFailurePresentation(
  failureCode: unknown,
  retryable: boolean,
): RuntimeFailurePresentation {
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
    fallback: 'The model service could not finish this task. Check the configuration before retrying.',
  };
}
