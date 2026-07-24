import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import type {
  FileVersionContent,
  FileVersionPage,
  FileVersionRestoreRequest,
  FileVersionRestoreResult,
  FileVersionSummary,
} from '../api/domains/files';
import { buildFileVersionRestoreRequest } from './FileVersionHistoryPanel';

const FILE_VERSION_PAGE_SIZE = 20;

export type FileVersionApi = {
  versions?: (path: string, offset: number, limit: number) => Promise<FileVersionPage>;
  readVersion?: (path: string, versionId: string) => Promise<FileVersionContent>;
  restoreVersion?: (
    path: string,
    versionId: string,
    request: FileVersionRestoreRequest,
  ) => Promise<FileVersionRestoreResult>;
  downloadVersion?: (path: string, versionId: string) => Promise<Blob>;
};

type UseFileVersionHistoryOptions = {
  api: FileVersionApi;
  path: string | null;
  onRestored: (content: string) => void;
  onDeleted: () => void;
  onDownload: (blob: Blob, filename: string) => void;
  onRefresh?: () => void;
  showToast: (message: string, type?: 'success' | 'error') => void;
};

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

export function useFileVersionHistory({
  api,
  path,
  onRestored,
  onDeleted,
  onDownload,
  onRefresh,
  showToast,
}: UseFileVersionHistoryOptions) {
  const { t } = useTranslation();
  const available = Boolean(api.versions && api.readVersion && api.restoreVersion);
  const [open, setOpen] = useState(false);
  const [historyPage, setHistoryPage] = useState<FileVersionPage | null>(null);
  const [selected, setSelected] = useState<FileVersionContent | null>(null);
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(null);
  const [restoreCandidate, setRestoreCandidate] = useState<FileVersionSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [selectedLoading, setSelectedLoading] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setOpen(false);
    setHistoryPage(null);
    setSelected(null);
    setSelectedVersionId(null);
    setRestoreCandidate(null);
    setError(null);
  }, [path]);

  const load = useCallback(async (offset = 0) => {
    if (!path || !api.versions) return;
    setLoading(true);
    setError(null);
    try {
      const next = await api.versions(path, offset, FILE_VERSION_PAGE_SIZE);
      setHistoryPage((current) => {
        if (!current || offset === 0) return next;
        return {
          ...next,
          offset: 0,
          versions: [...current.versions, ...next.versions],
        };
      });
    } catch (loadError) {
      setError(errorMessage(loadError, t('agent.workspace.fileHistory.loadFailed')));
    } finally {
      setLoading(false);
    }
  }, [api, path, t]);

  const openHistory = useCallback(() => {
    if (!available) return;
    setOpen(true);
    void load(0);
  }, [available, load]);

  const close = useCallback(() => {
    setOpen(false);
    setRestoreCandidate(null);
  }, []);

  const selectVersion = useCallback(async (version: FileVersionSummary) => {
    if (!path || !api.readVersion || version.state === 'unavailable') return;
    setSelectedVersionId(version.version_id);
    setSelected(null);
    setSelectedLoading(true);
    setError(null);
    try {
      setSelected(await api.readVersion(path, version.version_id));
    } catch (readError) {
      setError(errorMessage(readError, t('agent.workspace.fileHistory.readFailed')));
    } finally {
      setSelectedLoading(false);
    }
  }, [api, path, t]);

  const confirmRestore = useCallback(async () => {
    if (!path || !historyPage || !restoreCandidate || !api.restoreVersion) return;
    setRestoring(true);
    setError(null);
    try {
      const result = await api.restoreVersion(
        path,
        restoreCandidate.version_id,
        buildFileVersionRestoreRequest(historyPage),
      );
      setRestoreCandidate(null);
      if (result.current.exists) {
        onRestored(selected?.content || '');
        await load(0);
      } else {
        onDeleted();
        close();
      }
      onRefresh?.();
      showToast(t('agent.workspace.fileHistory.restored'));
    } catch (restoreError) {
      const message = errorMessage(restoreError, t('agent.workspace.fileHistory.restoreFailed'));
      setError(message);
      showToast(message, 'error');
      await load(0);
    } finally {
      setRestoring(false);
    }
  }, [
    api,
    close,
    historyPage,
    load,
    onDeleted,
    onRefresh,
    onRestored,
    path,
    restoreCandidate,
    selected,
    showToast,
    t,
  ]);

  const downloadVersion = useCallback(async (version: FileVersionSummary) => {
    if (!path || !api.downloadVersion) return;
    try {
      const blob = await api.downloadVersion(path, version.version_id);
      onDownload(blob, path.split('/').pop() || 'download');
    } catch (downloadError) {
      showToast(errorMessage(downloadError, t('agent.workspace.fileHistory.downloadFailed')), 'error');
    }
  }, [api, onDownload, path, showToast, t]);

  return {
    available,
    open,
    historyPage,
    selected,
    selectedVersionId,
    restoreCandidate,
    loading,
    selectedLoading,
    restoring,
    error,
    openHistory,
    close,
    retry: () => void load(0),
    selectVersion,
    requestRestore: setRestoreCandidate,
    confirmRestore: () => void confirmRestore(),
    cancelRestore: () => setRestoreCandidate(null),
    loadMore: () => void load(historyPage?.versions.length || 0),
    downloadVersion: (version: FileVersionSummary) => void downloadVersion(version),
  };
}
