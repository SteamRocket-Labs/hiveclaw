import { useEffect, useRef, useState } from 'react';

import {
  CHAT_SOCKET_KEEPALIVE_INTERVAL_MS,
  buildChatSocketKeepaliveMessage,
  type RuntimePhase,
} from './chatRuntime';
import {
  CHAT_TRANSPORT_DEGRADED_AFTER_ATTEMPTS,
  isSessionSocketAuthClose,
  reconnectDelayMs,
  shouldReconnectSessionSocket,
  transportPollIntervalMs,
  type ChatTransportPhase,
} from './chatTransportRecovery';
import {
  beginConnectionAttempt,
  buildSessionSubscribeMessage,
  connectionClosed,
  connectionFailed,
  createSessionConnectionState,
  observeHighestContiguousSequence,
  parseSessionReady,
  receiveSessionReady,
  type SessionConnectionState,
} from './sessionConnectionStore';

type SessionRuntimeKey = string;

export interface SessionSocketMessageContext {
  data: any;
  session: any;
  agentId: string;
  sessionId: string;
  key: SessionRuntimeKey;
  isActiveRuntime: boolean;
  closeSessionSocket: (key: SessionRuntimeKey, disableReconnect?: boolean) => void;
  failAuthentication: (key: SessionRuntimeKey, expired?: boolean) => void;
}

export interface SessionTransportCallbacks {
  onBackfill: (session: any, agentId: string) => number | void | Promise<number | void>;
  onLiveTailReady: (input: {
    key: SessionRuntimeKey;
    sessionId: string;
    acceptedAfterSequence: number;
  }) => void;
  onDisconnected: (input: {
    key: SessionRuntimeKey;
    phase: RuntimePhase;
    isActiveRuntime: boolean;
  }) => void;
  onMessage: (context: SessionSocketMessageContext) => void;
}

interface SessionTransportControllerOptions {
  enabled: boolean;
  agentId?: string;
  token?: string | null;
  activeSession: any | null;
  writableSession: boolean;
  isRunActive: (key: SessionRuntimeKey) => boolean;
  shouldKeepalive: (key: SessionRuntimeKey) => boolean;
  getHighestContiguousSequence: (key: SessionRuntimeKey) => number;
  getLiveSubscriptionCursor: (key: SessionRuntimeKey) => number | null;
  getProjectionPhase: (key: SessionRuntimeKey) => SessionConnectionState['projection']['phase'] | undefined;
  needsProjectionRecovery: (key: SessionRuntimeKey) => boolean;
  onAgentExpired: () => void;
  onSocketDisposed: (key: SessionRuntimeKey) => void;
  callbacks: SessionTransportCallbacks;
}

const runtimeKey = (agentId: string, sessionId: string) => `${agentId}:${sessionId}`;

function newConnectionAttemptId(): string {
  if (typeof globalThis.crypto?.randomUUID === 'function') return globalThis.crypto.randomUUID();
  return `connection-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function useSessionTransportController(options: SessionTransportControllerOptions) {
  const optionsRef = useRef(options);
  optionsRef.current = options;

  const socketsRef = useRef<Record<SessionRuntimeKey, WebSocket>>({});
  const reconnectTimersRef = useRef<Record<SessionRuntimeKey, ReturnType<typeof setTimeout> | null>>({});
  const keepaliveTimersRef = useRef<Record<SessionRuntimeKey, ReturnType<typeof setInterval> | null>>({});
  const reconnectDisabledRef = useRef<Record<SessionRuntimeKey, boolean>>({});
  const reconnectAttemptsRef = useRef<Record<SessionRuntimeKey, number>>({});
  const projectionRecoveryInFlightRef = useRef<Set<SessionRuntimeKey>>(new Set());
  const connectionStatesRef = useRef<Record<SessionRuntimeKey, SessionConnectionState>>({});
  const activeSocketRef = useRef<WebSocket | null>(null);
  const [wsConnected, setWsConnected] = useState(false);
  const [transportPhase, setTransportPhase] = useState<ChatTransportPhase>('initializing');
  const [transportReconnectAttempt, setTransportReconnectAttempt] = useState(0);

  const isActiveRuntime = (agentId: string, sessionId: string) => (
    optionsRef.current.agentId === agentId
    && String(optionsRef.current.activeSession?.id || '') === sessionId
  );

  const connectionState = (key: SessionRuntimeKey) => {
    const current = connectionStatesRef.current[key];
    if (current) return current;
    const created = createSessionConnectionState(
      optionsRef.current.getHighestContiguousSequence(key),
    );
    connectionStatesRef.current[key] = created;
    return created;
  };

  const commitConnectionState = (
    key: SessionRuntimeKey,
    state: SessionConnectionState,
    agentId: string,
    sessionId: string,
  ) => {
    connectionStatesRef.current[key] = state;
    if (!isActiveRuntime(agentId, sessionId)) return;
    setTransportPhase(state.transport.phase);
    setTransportReconnectAttempt(Math.max(0, state.transport.attempt - 1));
    setWsConnected(state.transport.phase === 'connected');
  };

  const syncProjectionCursor = (key: SessionRuntimeKey, agentId: string, sessionId: string) => {
    const next = observeHighestContiguousSequence(
      connectionState(key),
      optionsRef.current.getHighestContiguousSequence(key),
      optionsRef.current.getProjectionPhase(key),
    );
    commitConnectionState(key, next, agentId, sessionId);
  };

  const recoverProjectionIfNeeded = (
    key: SessionRuntimeKey,
    session: any,
    agentId: string,
    sessionId: string,
  ) => {
    if (!optionsRef.current.needsProjectionRecovery(key) || projectionRecoveryInFlightRef.current.has(key)) return;
    projectionRecoveryInFlightRef.current.add(key);
    void Promise.resolve(optionsRef.current.callbacks.onBackfill(session, agentId))
      .then(() => {
        syncProjectionCursor(key, agentId, sessionId);
        if (optionsRef.current.needsProjectionRecovery(key)) {
          const attemptId = connectionState(key).transport.connectionAttemptId;
          if (attemptId) {
            commitConnectionState(
              key,
              connectionFailed(connectionState(key), attemptId, 'degraded'),
              agentId,
              sessionId,
            );
          }
        }
      })
      .catch(() => {
        const attemptId = connectionState(key).transport.connectionAttemptId;
        if (attemptId) {
          commitConnectionState(
            key,
            connectionFailed(connectionState(key), attemptId, 'degraded'),
            agentId,
            sessionId,
          );
        }
      })
      .finally(() => projectionRecoveryInFlightRef.current.delete(key));
  };

  const clearReconnectTimer = (key: SessionRuntimeKey) => {
    const timer = reconnectTimersRef.current[key];
    if (timer) clearTimeout(timer);
    reconnectTimersRef.current[key] = null;
  };

  const clearKeepaliveTimer = (key: SessionRuntimeKey) => {
    const timer = keepaliveTimersRef.current[key];
    if (timer) clearInterval(timer);
    keepaliveTimersRef.current[key] = null;
  };

  const startKeepaliveTimer = (key: SessionRuntimeKey, socket: WebSocket) => {
    clearKeepaliveTimer(key);
    keepaliveTimersRef.current[key] = setInterval(() => {
      if (socketsRef.current[key] !== socket || socket.readyState !== WebSocket.OPEN) {
        clearKeepaliveTimer(key);
        return;
      }
      if (!optionsRef.current.shouldKeepalive(key)) return;
      socket.send(JSON.stringify(buildChatSocketKeepaliveMessage()));
    }, CHAT_SOCKET_KEEPALIVE_INTERVAL_MS);
  };

  const closeSessionSocket = (key: SessionRuntimeKey, disableReconnect = true) => {
    if (disableReconnect) reconnectDisabledRef.current[key] = true;
    clearReconnectTimer(key);
    clearKeepaliveTimer(key);
    delete reconnectAttemptsRef.current[key];
    const socket = socketsRef.current[key];
    if (socket) {
      socket.onclose = null;
      socket.onerror = null;
      socket.onmessage = null;
      socket.onopen = null;
      if (socket.readyState !== WebSocket.CLOSED) socket.close();
    }
    delete socketsRef.current[key];
    delete connectionStatesRef.current[key];
    optionsRef.current.onSocketDisposed(key);
  };

  const failAuthentication = (key: SessionRuntimeKey, expired = false) => {
    reconnectDisabledRef.current[key] = true;
    clearReconnectTimer(key);
    const [agentId = '', sessionId = ''] = key.split(':', 2);
    if (isActiveRuntime(agentId, sessionId)) {
      const attemptId = connectionState(key).transport.connectionAttemptId;
      const next = attemptId
        ? connectionFailed(connectionState(key), attemptId, 'auth_failed')
        : { ...connectionState(key), transport: { ...connectionState(key).transport, phase: 'auth_failed' as const } };
      commitConnectionState(key, next, agentId, sessionId);
    }
    if (expired) optionsRef.current.onAgentExpired();
  };

  const syncActiveSocketState = (
    session: any | null = optionsRef.current.activeSession,
    agentId: string | undefined = optionsRef.current.agentId,
    writableSession: boolean = optionsRef.current.writableSession,
  ) => {
    if (!session || !agentId || !writableSession) {
      activeSocketRef.current = null;
      setWsConnected(false);
      return;
    }
    const key = runtimeKey(agentId, String(session.id));
    const socket = socketsRef.current[key];
    activeSocketRef.current = socket ?? null;
    const state = connectionState(key);
    setWsConnected(Boolean(socket && state.transport.phase === 'connected'));
    setTransportReconnectAttempt(Math.max(0, state.transport.attempt - 1));
    setTransportPhase(state.transport.phase);
  };

  const ensureSessionSocket = (session: any, agentId: string, authToken: string) => {
    const sessionId = String(session.id);
    const key = runtimeKey(agentId, sessionId);
    if (reconnectDisabledRef.current[key]) return;
    const existing = socketsRef.current[key];
    if (
      existing
      && (existing.readyState === WebSocket.OPEN || existing.readyState === WebSocket.CONNECTING)
    ) return;

    const scheduleReconnect = () => {
      if (reconnectDisabledRef.current[key]) return;
      if (typeof navigator !== 'undefined' && navigator.onLine === false) {
        clearReconnectTimer(key);
        if (isActiveRuntime(agentId, sessionId)) {
          const attemptId = connectionState(key).transport.connectionAttemptId;
          const next = attemptId
            ? connectionFailed(connectionState(key), attemptId, 'offline')
            : connectionState(key);
          commitConnectionState(key, next, agentId, sessionId);
        }
        return;
      }
      const previousAttempts = reconnectAttemptsRef.current[key] ?? 0;
      const attempts = previousAttempts + 1;
      reconnectAttemptsRef.current[key] = attempts;
      if (isActiveRuntime(agentId, sessionId)) {
        setTransportReconnectAttempt(attempts);
        if (attempts >= CHAT_TRANSPORT_DEGRADED_AFTER_ATTEMPTS) {
          const attemptId = connectionState(key).transport.connectionAttemptId;
          if (attemptId) {
            commitConnectionState(
              key,
              connectionFailed(connectionState(key), attemptId, 'degraded'),
              agentId,
              sessionId,
            );
          }
        }
      }
      clearReconnectTimer(key);
      reconnectTimersRef.current[key] = setTimeout(() => {
        reconnectTimersRef.current[key] = null;
        if (!reconnectDisabledRef.current[key]) {
          void hydrateAndConnect(session, agentId, authToken);
        }
      }, reconnectDelayMs(previousAttempts));
    };

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    let socket: WebSocket;
    try {
      socket = new WebSocket(
        `${protocol}//${window.location.host}/ws/chat/${agentId}?token=${authToken}&session_id=${sessionId}`,
      );
    } catch (error) {
      console.warn(`WebSocket setup failed for session ${sessionId}:`, error);
      scheduleReconnect();
      return;
    }
    socketsRef.current[key] = socket;
    const attemptId = newConnectionAttemptId();
    commitConnectionState(
      key,
      beginConnectionAttempt(connectionState(key), attemptId),
      agentId,
      sessionId,
    );
    let subscribedAfterSequence: number | null | undefined;

    socket.onopen = () => {
      if (reconnectDisabledRef.current[key]) {
        socket.close();
        return;
      }
      startKeepaliveTimer(key, socket);
      if (isActiveRuntime(agentId, sessionId)) {
        activeSocketRef.current = socket;
      }
      subscribedAfterSequence = optionsRef.current.getLiveSubscriptionCursor(key);
      socket.send(JSON.stringify(buildSessionSubscribeMessage(
        sessionId,
        subscribedAfterSequence,
        attemptId,
      )));
    };

    socket.onclose = (event) => {
      if (socketsRef.current[key] === socket) delete socketsRef.current[key];
      clearKeepaliveTimer(key);
      const active = isActiveRuntime(agentId, sessionId);
      const phase: RuntimePhase = optionsRef.current.isRunActive(key) ? 'resuming' : 'idle';
      optionsRef.current.callbacks.onDisconnected({ key, phase, isActiveRuntime: active });
      const reconnect = shouldReconnectSessionSocket(
        event.code,
        Boolean(reconnectDisabledRef.current[key]),
      );
      const next = connectionClosed(connectionState(key), attemptId, reconnect);
      commitConnectionState(key, next, agentId, sessionId);
      if (active) activeSocketRef.current = null;
      if (isSessionSocketAuthClose(event.code)) {
        failAuthentication(key, event.code === 4003);
        return;
      }
      if (reconnect) scheduleReconnect();
    };

    socket.onerror = (error) => {
      console.warn(`WebSocket error for session ${sessionId}:`, error);
    };

    socket.onmessage = (event) => {
      let data: any;
      try {
        data = JSON.parse(event.data);
      } catch (error) {
        console.warn(`Invalid WebSocket payload for session ${sessionId}:`, error);
        return;
      }
      if (data?.type === 'session.ready') {
        try {
          if (subscribedAfterSequence === undefined) throw new Error('session_ready_before_subscribe');
          const ready = parseSessionReady(data, sessionId, attemptId, subscribedAfterSequence);
          reconnectAttemptsRef.current[key] = 0;
          let readyState = connectionState(key);
          if (ready.cursorMode === 'live_tail') {
            optionsRef.current.callbacks.onLiveTailReady({
              key,
              sessionId,
              acceptedAfterSequence: ready.acceptedAfterSequence,
            });
            readyState = observeHighestContiguousSequence(
              readyState,
              ready.acceptedAfterSequence,
              'current',
            );
          }
          commitConnectionState(
            key,
            receiveSessionReady(
              readyState,
              attemptId,
              ready.subscriptionId,
              ready.lastCommittedSequence,
            ),
            agentId,
            sessionId,
          );
        } catch (error) {
          console.warn(`Invalid Session ready frame for ${sessionId}:`, error);
          reconnectDisabledRef.current[key] = true;
          socket.close(4406, 'schema_unsupported');
        }
        return;
      }
      if (data?.type === 'session.error') {
        const retryable = data.error?.retryable === true;
        const code = typeof data.error?.code === 'string' ? data.error.code : 'event_store_retryable';
        if (!retryable) reconnectDisabledRef.current[key] = true;
        optionsRef.current.callbacks.onMessage({
          data,
          session,
          agentId,
          sessionId,
          key,
          isActiveRuntime: isActiveRuntime(agentId, sessionId),
          closeSessionSocket,
          failAuthentication,
        });
        if (code === 'auth_failed') failAuthentication(key, false);
        return;
      }
      optionsRef.current.callbacks.onMessage({
        data,
        session,
        agentId,
        sessionId,
        key,
        isActiveRuntime: isActiveRuntime(agentId, sessionId),
        closeSessionSocket,
        failAuthentication,
      });
      syncProjectionCursor(key, agentId, sessionId);
      recoverProjectionIfNeeded(key, session, agentId, sessionId);
    };
  };

  const hydrateAndConnect = async (session: any, agentId: string, authToken: string) => {
    const sessionId = String(session.id);
    const key = runtimeKey(agentId, sessionId);
    void Promise.resolve(optionsRef.current.callbacks.onBackfill(session, agentId))
      .then(() => syncProjectionCursor(key, agentId, sessionId))
      .catch((error) => {
        // Historical recovery and live delivery are independent. A failed
        // REST page remains observable in the projection UI, but must never
        // prevent the typed WebSocket subscription from becoming ready.
        console.warn(`Session history recovery failed for ${sessionId}:`, error);
      });
    if (!reconnectDisabledRef.current[key]) ensureSessionSocket(session, agentId, authToken);
  };

  const reconnectActiveTransport = () => {
    const { agentId, token, activeSession, writableSession } = optionsRef.current;
    if (!agentId || !token || !activeSession?.id || !writableSession) return;
    const key = runtimeKey(agentId, String(activeSession.id));
    reconnectDisabledRef.current[key] = false;
    reconnectAttemptsRef.current[key] = 0;
    clearReconnectTimer(key);
    clearKeepaliveTimer(key);
    const existing = socketsRef.current[key];
    if (existing) {
      existing.onclose = null;
      existing.onerror = null;
      existing.onmessage = null;
      existing.onopen = null;
      delete socketsRef.current[key];
      if (existing.readyState !== WebSocket.CLOSED) existing.close();
    }
    activeSocketRef.current = null;
    setWsConnected(false);
    setTransportReconnectAttempt(0);
    connectionStatesRef.current[key] = createSessionConnectionState(
      optionsRef.current.getHighestContiguousSequence(key),
    );
    setTransportPhase(typeof navigator !== 'undefined' && navigator.onLine === false ? 'offline' : 'initializing');
    void hydrateAndConnect(activeSession, agentId, token);
  };

  const resetActiveTransportState = () => {
    activeSocketRef.current = null;
    setWsConnected(false);
    setTransportPhase('initializing');
    setTransportReconnectAttempt(0);
  };

  const getSessionSocket = (agentId: string, sessionId: string) => socketsRef.current[runtimeKey(agentId, sessionId)] ?? null;

  useEffect(() => {
    const { enabled, agentId, token, activeSession, writableSession } = optionsRef.current;
    if (!enabled || !agentId || !token) return;
    if (!activeSession || !writableSession) {
      syncActiveSocketState(activeSession, agentId);
      return;
    }
    const key = runtimeKey(agentId, String(activeSession.id));
    reconnectDisabledRef.current[key] = false;
    reconnectAttemptsRef.current[key] = 0;
    setTransportReconnectAttempt(0);
    const prior = connectionState(key);
    setTransportPhase(
      typeof navigator !== 'undefined' && navigator.onLine === false
        ? 'offline'
        : prior.transport.everReady ? 'reconnecting' : 'initializing',
    );
    void hydrateAndConnect(activeSession, agentId, token);
    syncActiveSocketState(activeSession, agentId);
  }, [options.enabled, options.agentId, options.token, options.activeSession?.id, options.activeSession?.operator_view, options.writableSession]);

  useEffect(() => {
    const { enabled, agentId, token, activeSession, writableSession } = optionsRef.current;
    if (!enabled || !agentId || !token || !activeSession?.id || !writableSession) return;
    const key = runtimeKey(agentId, String(activeSession.id));
    const wake = () => {
      if (reconnectDisabledRef.current[key]) return;
      clearReconnectTimer(key);
      setTransportPhase(connectionState(key).transport.everReady ? 'reconnecting' : 'initializing');
      setTransportReconnectAttempt(reconnectAttemptsRef.current[key] || 0);
      void hydrateAndConnect(activeSession, agentId, token);
      // Browsers may emit an offline/online pair without closing an already
      // open WebSocket. In that case ensureSessionSocket is intentionally a
      // no-op, so resync the existing socket instead of leaving the UI stuck
      // in "reconnecting" forever.
      syncActiveSocketState(activeSession, agentId);
    };
    const handleOffline = () => {
      clearReconnectTimer(key);
      const attemptId = connectionState(key).transport.connectionAttemptId;
      if (attemptId) {
        commitConnectionState(
          key,
          connectionFailed(connectionState(key), attemptId, 'offline'),
          agentId,
          String(activeSession.id),
        );
      } else {
        setWsConnected(false);
        setTransportPhase('offline');
      }
    };
    const handleVisibility = () => {
      if (document.visibilityState !== 'visible') return;
      void Promise.resolve(optionsRef.current.callbacks.onBackfill(activeSession, agentId)).then(() => {
        syncProjectionCursor(key, agentId, String(activeSession.id));
      });
      const socket = socketsRef.current[key];
      if (!socket || socket.readyState === WebSocket.CLOSED || socket.readyState === WebSocket.CLOSING) wake();
    };
    window.addEventListener('online', wake);
    window.addEventListener('offline', handleOffline);
    document.addEventListener('visibilitychange', handleVisibility);
    return () => {
      window.removeEventListener('online', wake);
      window.removeEventListener('offline', handleOffline);
      document.removeEventListener('visibilitychange', handleVisibility);
    };
  }, [options.enabled, options.agentId, options.token, options.activeSession?.id, options.activeSession?.operator_view, options.writableSession]);

  useEffect(() => {
    const { enabled, agentId, activeSession, writableSession } = optionsRef.current;
    if (!enabled || !agentId || !activeSession?.id || !writableSession) return;
    const key = runtimeKey(agentId, String(activeSession.id));
    const interval = transportPollIntervalMs(transportPhase, optionsRef.current.isRunActive(key));
    if (interval === null) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const poll = async () => {
      if (cancelled) return;
      if (typeof navigator === 'undefined' || navigator.onLine !== false) {
        await optionsRef.current.callbacks.onBackfill(activeSession, agentId);
        syncProjectionCursor(key, agentId, String(activeSession.id));
      }
      if (cancelled) return;
      const nextInterval = transportPollIntervalMs(transportPhase, optionsRef.current.isRunActive(key));
      if (nextInterval !== null) timer = setTimeout(poll, nextInterval);
    };
    timer = setTimeout(poll, interval);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [options.enabled, options.agentId, options.activeSession?.id, options.activeSession?.operator_view, options.writableSession, transportPhase]);

  useEffect(() => () => {
    Object.keys(reconnectDisabledRef.current).forEach((key) => { reconnectDisabledRef.current[key] = true; });
    Object.keys(reconnectTimersRef.current).forEach(clearReconnectTimer);
    Object.keys(keepaliveTimersRef.current).forEach(clearKeepaliveTimer);
    projectionRecoveryInFlightRef.current.clear();
    Object.values(socketsRef.current).forEach((socket) => {
      socket.onclose = null;
      socket.onerror = null;
      socket.onmessage = null;
      socket.onopen = null;
      if (socket.readyState !== WebSocket.CLOSED) socket.close();
    });
    socketsRef.current = {};
    activeSocketRef.current = null;
  }, []);

  return {
    wsConnected,
    transportPhase,
    transportReconnectAttempt,
    activeSocketRef,
    closeSessionSocket,
    getSessionSocket,
    reconnectActiveTransport,
    resetActiveTransportState,
    syncActiveSocketState,
  };
}
