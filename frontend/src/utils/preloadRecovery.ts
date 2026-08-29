const PRELOAD_RECOVERY_STORAGE_KEY = 'hive.stale_preload_recovery';
const PRELOAD_RECOVERY_WINDOW_MS = 30_000;

type PreloadRecoveryEvent = Pick<Event, 'preventDefault'>;

type PreloadRecoveryEnvironment = {
  route: string;
  storage: Pick<Storage, 'getItem' | 'setItem'>;
  reload: () => void;
  now?: () => number;
};

type PreloadRecoveryMarker = {
  route: string;
  at: number;
};

export function attemptStalePreloadRecovery(
  event: PreloadRecoveryEvent,
  environment: PreloadRecoveryEnvironment,
): boolean {
  const now = (environment.now ?? Date.now)();
  try {
    const rawMarker = environment.storage.getItem(PRELOAD_RECOVERY_STORAGE_KEY);
    const marker = rawMarker ? JSON.parse(rawMarker) as Partial<PreloadRecoveryMarker> : null;
    const recentlyRetried = marker?.route === environment.route
      && typeof marker.at === 'number'
      && now - marker.at < PRELOAD_RECOVERY_WINDOW_MS;
    if (recentlyRetried) return false;
    environment.storage.setItem(
      PRELOAD_RECOVERY_STORAGE_KEY,
      JSON.stringify({ route: environment.route, at: now } satisfies PreloadRecoveryMarker),
    );
  } catch {
    // Without a durable loop guard, let the error boundary offer a manual
    // recovery instead of risking repeated automatic reloads.
    return false;
  }

  event.preventDefault();
  environment.reload();
  return true;
}

export function installStalePreloadRecovery(): void {
  window.addEventListener('vite:preloadError', (event) => {
    let storage: Storage;
    try {
      storage = window.sessionStorage;
    } catch {
      return;
    }
    attemptStalePreloadRecovery(event, {
      route: `${window.location.pathname}${window.location.search}${window.location.hash}`,
      storage,
      reload: () => window.location.reload(),
    });
  });
}
