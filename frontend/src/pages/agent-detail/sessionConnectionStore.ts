import type { ChatTransportPhase } from './chatTransportRecovery';

export type SessionTransportState = {
  phase: ChatTransportPhase;
  attempt: number;
  everReady: boolean;
  connectionAttemptId?: string;
  subscriptionId?: string;
};

export type SessionProjectionSyncState = {
  phase: 'hydrating' | 'catching_up' | 'current' | 'gap_detected' | 'stale';
  highestContiguousSequence: number;
  serverLastCommittedSequence?: number;
};

export type SessionConnectionState = {
  transport: SessionTransportState;
  projection: SessionProjectionSyncState;
};

export type SessionReadyFrame = {
  subscriptionId: string;
  acceptedAfterSequence: number;
  lastCommittedSequence: number;
  connectionAttemptId: string;
};

export function buildSessionSubscribeMessage(
  sessionId: string,
  afterSequence: number,
  connectionAttemptId: string,
): Record<string, string | number> {
  return {
    type: 'session.subscribe',
    session_id: sessionId,
    after_sequence: Math.max(0, Math.floor(afterSequence)),
    schema_version: 2,
    connection_attempt_id: connectionAttemptId,
  };
}

export function parseSessionReady(
  value: unknown,
  expectedSessionId: string,
  expectedConnectionAttemptId: string,
  expectedAfterSequence: number,
): SessionReadyFrame {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('invalid_session_ready');
  }
  const frame = value as Record<string, unknown>;
  if (frame.type !== 'session.ready' || frame.schema_version !== 2) {
    throw new Error('invalid_session_ready');
  }
  if (frame.session_id !== expectedSessionId) throw new Error('session_ready_session_mismatch');
  if (frame.connection_attempt_id !== expectedConnectionAttemptId) {
    throw new Error('session_ready_attempt_mismatch');
  }
  const subscriptionId = typeof frame.subscription_id === 'string' ? frame.subscription_id : '';
  const acceptedAfterSequence = Number(frame.accepted_after_sequence);
  const lastCommittedSequence = Number(frame.last_committed_sequence);
  if (
    !subscriptionId
    || !Number.isSafeInteger(acceptedAfterSequence)
    || acceptedAfterSequence < 0
    || !Number.isSafeInteger(lastCommittedSequence)
    || lastCommittedSequence < acceptedAfterSequence
  ) {
    throw new Error('invalid_session_ready');
  }
  if (acceptedAfterSequence !== expectedAfterSequence) {
    throw new Error('session_ready_cursor_mismatch');
  }
  return {
    subscriptionId,
    acceptedAfterSequence,
    lastCommittedSequence,
    connectionAttemptId: expectedConnectionAttemptId,
  };
}

export function createSessionConnectionState(
  highestContiguousSequence = 0,
): SessionConnectionState {
  return {
    transport: { phase: 'initializing', attempt: 0, everReady: false },
    projection: {
      phase: 'hydrating',
      highestContiguousSequence,
    },
  };
}

export function beginConnectionAttempt(
  state: SessionConnectionState,
  connectionAttemptId: string,
): SessionConnectionState {
  const attempt = state.transport.attempt + 1;
  return {
    ...state,
    transport: {
      phase: state.transport.everReady ? 'reconnecting' : 'initializing',
      attempt,
      everReady: state.transport.everReady,
      connectionAttemptId,
    },
    projection: {
      ...state.projection,
      phase: state.transport.everReady ? 'catching_up' : 'hydrating',
    },
  };
}

export function receiveSessionReady(
  state: SessionConnectionState,
  connectionAttemptId: string,
  subscriptionId: string,
  serverLastCommittedSequence: number,
): SessionConnectionState {
  if (state.transport.connectionAttemptId !== connectionAttemptId) return state;
  const current = state.projection.highestContiguousSequence >= serverLastCommittedSequence;
  return {
    transport: {
      phase: 'connected',
      attempt: state.transport.attempt,
      everReady: true,
      connectionAttemptId,
      subscriptionId,
    },
    projection: {
      ...state.projection,
      phase: current ? 'current' : 'catching_up',
      serverLastCommittedSequence,
    },
  };
}

export function observeHighestContiguousSequence(
  state: SessionConnectionState,
  highestContiguousSequence: number,
  projectionPhase?: SessionProjectionSyncState['phase'],
): SessionConnectionState {
  const serverSequence = state.projection.serverLastCommittedSequence;
  const phase = projectionPhase === 'gap_detected' || projectionPhase === 'stale'
    ? projectionPhase
    : serverSequence !== undefined && highestContiguousSequence >= serverSequence
      ? 'current'
      : state.transport.everReady
        ? 'catching_up'
        : 'hydrating';
  return {
    ...state,
    projection: {
      ...state.projection,
      phase,
      highestContiguousSequence,
    },
  };
}

export function connectionClosed(
  state: SessionConnectionState,
  connectionAttemptId: string,
  unexpected: boolean,
): SessionConnectionState {
  if (state.transport.connectionAttemptId !== connectionAttemptId) return state;
  return {
    ...state,
    transport: {
      phase: unexpected
        ? state.transport.everReady ? 'reconnecting' : 'initializing'
        : 'offline',
      attempt: state.transport.attempt,
      everReady: state.transport.everReady,
      connectionAttemptId,
    },
  };
}

export function connectionFailed(
  state: SessionConnectionState,
  connectionAttemptId: string,
  phase: 'degraded' | 'offline' | 'auth_failed',
): SessionConnectionState {
  if (state.transport.connectionAttemptId !== connectionAttemptId) return state;
  return {
    ...state,
    transport: {
      ...state.transport,
      phase,
    },
  };
}
