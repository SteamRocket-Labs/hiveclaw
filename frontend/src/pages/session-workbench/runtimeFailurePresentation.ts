// Frontend matches backend/app/services/runtime_display_status.py byte for byte.
export const EMPTY_SEARCH_MATERIAL_FOUND_MESSAGE = '已停止继续搜索，但没能整理出答复。你可以让我根据目前的结果直接作答。';

export type RuntimeFailurePresentation = {
  kind: 'model_missing' | 'quota_exhausted' | 'rate_limited' | 'auth_or_permission' | 'retryable' | 'unavailable' | 'delivery_unknown' | 'persistence_error' | 'timeout' | 'empty_search' | 'empty_search_summary_failed' | 'refused_result' | 'terminal_task_read' | 'running_task_read';
  fallback: string;
};

export function runtimeFailurePresentation(
  failureCode: unknown,
  retryable: boolean,
  deliveryState?: unknown,
  terminalReason?: unknown,
  message?: unknown,
): RuntimeFailurePresentation {
  if (terminalReason === 'persistence_error') {
    return {
      kind: 'persistence_error',
      fallback: 'This task did not finish. Please try again later.',
    };
  }
  if (failureCode === 'repeated_empty_search') {
    if (message === EMPTY_SEARCH_MATERIAL_FOUND_MESSAGE) {
      return {
        kind: 'empty_search_summary_failed',
        fallback: "I've stopped searching, but I couldn't put together a reply. You can ask me to answer directly using the results so far.",
      };
    }
    // R18a round 2: the loop guard stopped the turn after several consecutive
    // typed-empty searches; the plain sentence is the member's answer, and no
    // retry action is offered (retry_safe is false for this failure).
    return {
      kind: 'empty_search',
      fallback: 'Several searches found no matching material.',
    };
  }
  if (failureCode === 'identical_refused_result_read') {
    return {
      kind: 'refused_result',
      fallback: "This result couldn't be read safely this time, so I couldn't continue. You can ask me to arrange this work again.",
    };
  }
  if (failureCode === 'identical_terminal_task_read') {
    return {
      kind: 'terminal_task_read',
      fallback: 'The colleague’s task ended without success, so I stopped checking it. You can ask me to arrange it again.',
    };
  }
  if (failureCode === 'identical_running_task_read') {
    return {
      kind: 'running_task_read',
      fallback: 'The colleague is still working. This conversation will continue when the result arrives.',
    };
  }
  if (failureCode === 'timeout' && deliveryState === 'unknown') {
    // R18a round 2 (ruling 4): a timed-out stream that ends as a visible
    // failure is transient — the request was authenticated and streaming, so
    // naming a configuration problem would be untrue, and retrying the whole
    // turn is not offered because earlier rounds may have committed effects.
    return {
      kind: 'timeout',
      fallback: 'The model service timed out and this task did not finish. Please try again later.',
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
