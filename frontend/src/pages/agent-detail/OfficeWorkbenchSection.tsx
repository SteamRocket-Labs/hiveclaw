import React, { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { IconDeviceFloppy, IconFilePlus, IconPlayerPlay, IconRefresh } from '@tabler/icons-react';
import { useTranslation } from 'react-i18next';

import { officeApi, type OfficeEditorConfig, type OfficeKind } from '../../api/domains/office';
import './OfficeWorkbenchSection.css';

declare global {
  interface Window {
    DocsAPI?: {
      DocEditor: new (elementId: string, config: Record<string, unknown>) => { destroyEditor?: () => void };
    };
  }
}

type OfficeWorkbenchSectionProps = {
  agentId: string;
};

const DEFAULT_PATH = 'workspace/demo.docx';
const EDITOR_HEIGHT = 'clamp(560px, calc(100vh - 360px), 820px)';

function inferKind(path: string): OfficeKind {
  const lower = path.toLowerCase();
  if (lower.endsWith('.xlsx')) return 'xlsx';
  if (lower.endsWith('.pptx')) return 'pptx';
  return 'docx';
}

function getErrorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (error && typeof error === 'object' && 'message' in error) {
    return String((error as { message?: unknown }).message || '');
  }
  return '';
}

function OnlyOfficeHost({ config }: { config: Extract<OfficeEditorConfig, { enabled: true }> }) {
  const [loadError, setLoadError] = useState('');
  const containerId = useMemo(() => {
    const document = config.config.document as { key?: string } | undefined;
    return `onlyoffice-editor-${document?.key || 'document'}`;
  }, [config.config]);
  const editorConfig = useMemo(
    () => ({
      ...config.config,
      width: '100%',
      height: '100%',
    }),
    [config.config],
  );

  useEffect(() => {
    let cancelled = false;
    let editor: { destroyEditor?: () => void } | null = null;
    const scriptId = 'onlyoffice-docs-api';

    const mountEditor = () => {
      if (cancelled) return;
      setLoadError('');
      if (!window.DocsAPI?.DocEditor) {
        setLoadError('DocsAPI unavailable');
        return;
      }
      const host = document.getElementById(containerId);
      if (host) host.innerHTML = '';
      try {
        editor = new window.DocsAPI.DocEditor(containerId, editorConfig);
      } catch (error) {
        setLoadError(getErrorMessage(error) || 'DocsAPI failed to mount');
      }
    };

    const existing = document.getElementById(scriptId) as HTMLScriptElement | null;
    if (existing) {
      if (window.DocsAPI?.DocEditor) {
        mountEditor();
      } else {
        existing.addEventListener('load', mountEditor, { once: true });
      }
    } else {
      const script = document.createElement('script');
      script.id = scriptId;
      script.src = `${config.documentServerUrl.replace(/\/$/, '')}/web-apps/apps/api/documents/api.js`;
      script.async = true;
      script.onload = mountEditor;
      script.onerror = () => setLoadError('DocsAPI script failed');
      document.body.appendChild(script);
    }

    return () => {
      cancelled = true;
      editor?.destroyEditor?.();
    };
  }, [config.documentServerUrl, containerId, editorConfig]);

  return (
    <div className="office-workbench__editorShell" style={{ height: EDITOR_HEIGHT }}>
      {loadError ? (
        <div className="office-workbench-load-error">{loadError}</div>
      ) : (
        <div id={containerId} className="office-workbench-host" />
      )}
    </div>
  );
}

export default function OfficeWorkbenchSection({ agentId }: OfficeWorkbenchSectionProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [pathInput, setPathInput] = useState(DEFAULT_PATH);
  const [selectedPath, setSelectedPath] = useState('');

  const editorQuery = useQuery({
    queryKey: ['office-editor-config', agentId, selectedPath],
    queryFn: () => officeApi.getEditorConfig(agentId, selectedPath, 'edit'),
    enabled: Boolean(agentId && selectedPath.trim()),
    retry: false,
  });

  const createMutation = useMutation({
    mutationFn: () => {
      const normalizedPath = pathInput.trim();
      return officeApi.createDocument(agentId, { path: normalizedPath, kind: inferKind(normalizedPath) });
    },
    onSuccess: () => {
      const normalizedPath = pathInput.trim();
      setSelectedPath(normalizedPath);
      queryClient.invalidateQueries({ queryKey: ['office-editor-config', agentId, normalizedPath] });
    },
  });

  const forceSaveMutation = useMutation({
    mutationFn: () => officeApi.forceSave(agentId, selectedPath),
  });

  const config = editorQuery.data;
  const disabledConfig = config && !config.enabled ? config : null;
  const editorError = editorQuery.isError ? getErrorMessage(editorQuery.error) : '';

  return (
    <div className="office-workbench">
      <div className="office-workbench-body">
        <section className="office-workbench-toolbar">
          <div className="office-workbench-heading">
            <h3 className="office-workbench-title">{t('agent.office.title', 'Office')}</h3>
            <p className="office-workbench-subtitle">
              {t('agent.office.subtitle', 'DOCX, XLSX, and PPTX editing for this workspace')}
            </p>
          </div>

          <label className="office-workbench-field">
            {t('agent.office.pathLabel', 'Document path')}
            <input
              value={pathInput}
              onChange={(event) => setPathInput(event.target.value)}
              placeholder={DEFAULT_PATH}
              className="office-workbench-input"
            />
          </label>

          <div className="office-workbench-actions">
            <button className="btn btn-primary" onClick={() => setSelectedPath(pathInput.trim())} disabled={!pathInput.trim()}>
              <IconPlayerPlay size={15} stroke={1.7} />
              {t('agent.office.open', 'Open')}
            </button>
            <button
              className="btn btn-secondary"
              onClick={() => createMutation.mutateAsync()}
              disabled={!pathInput.trim() || createMutation.isPending}
            >
              <IconFilePlus size={15} stroke={1.7} />
              {t('agent.office.create', 'Create')}
            </button>
            <button className="btn btn-secondary" onClick={() => editorQuery.refetch()} disabled={editorQuery.isFetching}>
              <IconRefresh size={15} stroke={1.7} />
              {t('common.refresh', 'Refresh')}
            </button>
            <button
              className="btn btn-secondary"
              onClick={() => forceSaveMutation.mutateAsync()}
              disabled={!config?.enabled || forceSaveMutation.isPending}
            >
              <IconDeviceFloppy size={15} stroke={1.7} />
              {t('agent.office.save', 'Save')}
            </button>
          </div>
        </section>

        {disabledConfig && (
          <div className="office-workbench-notice">
            <strong className="office-workbench-notice-title">{t('agent.office.disabledTitle', 'ONLYOFFICE is not configured')}</strong>
            <div className="u-row u-tertiary">
              {(disabledConfig.required_env || []).join(', ') || disabledConfig.reason}
            </div>
          </div>
        )}

        <main className="office-workbench-main">
          {config?.enabled && !editorError ? (
            <OnlyOfficeHost config={config} />
          ) : editorError ? (
            <div className="office-workbench-placeholder office-workbench-placeholder--stack">
              <strong className="office-workbench-placeholder-title">
                {t('agent.office.documentMissingTitle', 'Office document not found')}
              </strong>
              <span>{t('agent.office.documentMissingMessage', 'Create it first, then open it again.')}</span>
            </div>
          ) : (
            <div className="office-workbench-placeholder">
              {editorQuery.isFetching
                ? t('common.loading', 'Loading...')
                : t('agent.office.emptyState', 'Open or create an Office document to start editing.')}
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
