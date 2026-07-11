import { useEffect, useRef, useState } from 'react';

import {
  CHAT_SOCKET_KEEPALIVE_INTERVAL_MS,
  buildChatSocketKeepaliveMessage,
  type RuntimePhase,
} from './chatRuntime';
import {
  chatTransportPhase,
  reconnectDelayMs,
  transportPollIntervalMs,
  type ChatTransportPhase,
} from './chatTransportRecovery';

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
  onBackfill: (session: any, agentId: string) => void | Promise<void>;
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
  onAgentExpired: () => void;
  onSocketDisposed: (key: SessionRuntimeKey) => void;
  callbacks: SessionTransportCallbacks;
}

const runtimeKey = (agentId: string, sessionId: string) => `${agentId}:${sessionId}`;

export function useSessionTransportController(options: SessionTransportControllerOptions) {
  const optionsRef = useRef(options);
  optionsRef.current = options;

  const socketsRef = useRef<Record<SessionRuntimeKey, WebSocket>>({});
  const reconnectTimersRef = useRef<Record<SessionRuntimeKey, ReturnType<typeof setTimeout> | null>>({});
  const keepaliveTimersRef = useRef<Record<SessionRuntimeKey, ReturnType<typeof setInterval> | null>>({});
  const reconnectDisabledRef = useRef<Record<SessionRuntimeKey, boolean>>({});
  const reconnectAttemptsRef = useRef<Record<SessionRuntimeKey, number>>({});
  const activeSocketRef = useRef<WebSocket | null>(null);
  const [wsConnected, setWsConnected] = useState(false);
  const [transportPhase, setTransportPhase] = useState<ChatTransportPhase>('reconnecting');
  const [transportReconnectAttempt, setTransportReconnectAttempt] = useState(0);

  const isActiveRuntime = (agentId: string, sessionId: string) => (
    optionsRef.current.agentId === agentId
    && String(optionsRef.current.activeSession?.id || '') === sessionId
  );

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
    if (socket && socket.readyState !== WebSocket.CLOSED) socket.close();
    delete socketsRef.current[key];
    optionsRef.current.onSocketDisposed(key);
  };

  const failAuthentication = (key: SessionRuntimeKey, expired = false) => {
    reconnectDisabledRef.current[key] = true;
    clearReconnectTimer(key);
    const [agentId = '', sessionId = ''] = key.split(':', 2);
    if (isActiveRuntime(agentId, sessionId)) {
      setWsConnected(false);
      setTransportPhase('auth_failed');
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
    const connected = Boolean(socket && socket.readyState === WebSocket.OPEN);
    const attempts = reconnectAttemptsRef.current[key] || 0;
    setWsConnected(connected);
    setTransportReconnectAttempt(attempts);
    setTransportPhase(chatTransportPhase({
      online: typeof navigator === 'undefined' || navigator.onLine !== false,
      connected,
      attempts,
      authFailed: Boolean(reconnectDisabledRef.current[key]),
    }));
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
          setTransportPhase('offline');
          setTransportReconnectAttempt(reconnectAttemptsRef.current[key] || 0);
        }
        return;
      }
      const previousAttempts = reconnectAttemptsRef.current[key] ?? 0;
      const attempts = previousAttempts + 1;
      reconnectAttemptsRef.current[key] = attempts;
      if (isActiveRuntime(agentId, sessionId)) {
        setTransportReconnectAttempt(attempts);
        setTransportPhase(chatTransportPhase({ online: true, connected: false, attempts }));
      }
      clearReconnectTimer(key);
      reconnectTimersRef.current[key] = setTimeout(() => {
        reconnectTimersRef.current[key] = null;
        if (!reconnectDisabledRef.current[key]) ensureSessionSocket(session, agentId, authToken);
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

    socket.onopen = () => {
      if (reconnectDisabledRef.current[key]) {
        socket.close();
        return;
      }
      reconnectAttemptsRef.current[key] = 0;
      startKeepaliveTimer(key, socket);
      if (isActiveRuntime(agentId, sessionId)) {
        activeSocketRef.current = socket;
        setWsConnected(true);
        setTransportPhase('connected');
        setTransportReconnectAttempt(0);
      }
      void optionsRef.current.callbacks.onBackfill(session, agentId);
    };

    socket.onclose = (event) => {
      if (socketsRef.current[key] === socket) delete socketsRef.current[key];
      clearKeepaliveTimer(key);
      const active = isActiveRuntime(agentId, sessionId);
      const phase: RuntimePhase = optionsRef.current.isRunActive(key) ? 'resuming' : 'idle';
      optionsRef.current.callbacks.onDisconnected({ key, phase, isActiveRuntime: active });
      if (active) {
        activeSocketRef.current = null;
        setWsConnected(false);
      }
      if (event.code === 4003 || event.code === 4002) {
        failAuthentication(key, event.code === 4003);
        return;
      }
      scheduleReconnect();
    };

    socket.onerror = (error) => {
      if (isActiveRuntime(agentId, sessionId)) setWsConnected(false);
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
    };
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
    setTransportPhase(typeof navigator !== 'undefined' && navigator.onLine === false ? 'offline' : 'reconnecting');
    void optionsRef.current.callbacks.onBackfill(activeSession, agentId);
    ensureSessionSocket(activeSession, agentId, token);
  };

  const resetActiveTransportState = () => {
    activeSocketRef.current = null;
    setWsConnected(false);
    setTransportPhase('reconnecting');
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
    setTransportPhase(typeof navigator !== 'undefined' && navigator.onLine === false ? 'offline' : 'reconnecting');
    ensureSessionSocket(activeSession, agentId, token);
    syncActiveSocketState(activeSession, agentId);
  }, [options.enabled, options.agentId, options.token, options.activeSession?.id, options.activeSession?.operator_view, options.writableSession]);

  useEffect(() => {
    const { enabled, agentId, token, activeSession, writableSession } = optionsRef.current;
    if (!enabled || !agentId || !token || !activeSession?.id || !writableSession) return;
    const key = runtimeKey(agentId, String(activeSession.id));
    const wake = () => {
      if (reconnectDisabledRef.current[key]) return;
      clearReconnectTimer(key);
      setTransportPhase('reconnecting');
      setTransportReconnectAttempt(reconnectAttemptsRef.current[key] || 0);
      void optionsRef.current.callbacks.onBackfill(activeSession, agentId);
      ensureSessionSocket(activeSession, agentId, token);
    };
    const handleOffline = () => {
      clearReconnectTimer(key);
      setWsConnected(false);
      setTransportPhase('offline');
    };
    const handleVisibility = () => {
      if (document.visibilityState !== 'visible') return;
      void optionsRef.current.callbacks.onBackfill(activeSession, agentId);
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
    Object.values(socketsRef.current).forEach((socket) => {
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
