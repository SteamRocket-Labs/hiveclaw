import React, { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { IconDeviceFloppy, IconFilePlus, IconPlayerPlay, IconRefresh } from '@tabler/icons-react';
import { useTranslation } from 'react-i18next';

import { officeApi, type OfficeEditorConfig, type OfficeKind } from '../../api/domains/office';

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

function inferKind(path: string): OfficeKind {
  const lower = path.toLowerCase();
  if (lower.endsWith('.xlsx')) return 'xlsx';
  if (lower.endsWith('.pptx')) return 'pptx';
  return 'docx';
}

function OnlyOfficeHost({ config }: { config: Extract<OfficeEditorConfig, { enabled: true }> }) {
  const [loadError, setLoadError] = useState('');
  const containerId = useMemo(() => {
    const document = config.config.document as { key?: string } | undefined;
    return `onlyoffice-editor-${document?.key || 'document'}`;
  }, [config.config]);

  useEffect(() => {
    let cancelled = false;
    let editor: { destroyEditor?: () => void } | null = null;
    const scriptId = 'onlyoffice-docs-api';

    const mountEditor = () => {
      if (cancelled) return;
      if (!window.DocsAPI?.DocEditor) {
        setLoadError('DocsAPI unavailable');
        return;
      }
      editor = new window.DocsAPI.DocEditor(containerId, config.config);
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
  }, [config, containerId]);

  return (
    <div style={{ minHeight: '620px', border: '1px solid var(--border-primary)', borderRadius: 8, overflow: 'hidden' }}>
      {loadError ? (
        <div style={{ padding: 16, color: 'var(--danger)' }}>{loadError}</div>
      ) : (
        <div id={containerId} style={{ width: '100%', height: 620 }} />
      )}
    </div>
  );
}

export default function OfficeWorkbenchSection({ agentId }: OfficeWorkbenchSectionProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [pathInput, setPathInput] = useState(DEFAULT_PATH);
  const [selectedPath, setSelectedPath] = useState(DEFAULT_PATH);

  const editorQuery = useQuery({
    queryKey: ['office-editor-config', agentId, selectedPath],
    queryFn: () => officeApi.getEditorConfig(agentId, selectedPath, 'edit'),
    enabled: Boolean(agentId && selectedPath),
    retry: false,
  });

  const createMutation = useMutation({
    mutationFn: () => officeApi.createDocument(agentId, { path: pathInput, kind: inferKind(pathInput) }),
    onSuccess: () => {
      setSelectedPath(pathInput);
      queryClient.invalidateQueries({ queryKey: ['office-editor-config', agentId, pathInput] });
    },
  });

  const forceSaveMutation = useMutation({
    mutationFn: () => officeApi.forceSave(agentId, selectedPath),
  });

  const config = editorQuery.data;
  const disabledConfig = config && !config.enabled ? config : null;

  return (
    <div style={{ padding: '20px 24px' }}>
      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(240px, 360px) minmax(0, 1fr)', gap: 20 }}>
        <aside style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div>
            <h3 style={{ margin: '0 0 6px', fontSize: 18 }}>{t('agent.office.title', 'Office')}</h3>
            <p style={{ margin: 0, color: 'var(--text-tertiary)', fontSize: 13 }}>
              {t('agent.office.subtitle', 'DOCX, XLSX, and PPTX editing for this workspace')}
            </p>
          </div>

          <label style={{ display: 'grid', gap: 6, fontSize: 13, color: 'var(--text-secondary)' }}>
            {t('agent.office.pathLabel', 'Document path')}
            <input
              value={pathInput}
              onChange={(event) => setPathInput(event.target.value)}
              placeholder={DEFAULT_PATH}
              style={{
                width: '100%',
                boxSizing: 'border-box',
                border: '1px solid var(--border-primary)',
                borderRadius: 6,
                padding: '8px 10px',
                background: 'var(--bg-elevated)',
                color: 'var(--text-primary)',
              }}
            />
          </label>

          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <button className="btn btn-primary" onClick={() => setSelectedPath(pathInput)} disabled={!pathInput.trim()}>
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

          {disabledConfig && (
            <div style={{ border: '1px solid var(--border-primary)', borderRadius: 8, padding: 12, color: 'var(--text-secondary)' }}>
              <strong style={{ display: 'block', marginBottom: 6 }}>{t('agent.office.disabledTitle', 'ONLYOFFICE is not configured')}</strong>
              <div style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>
                {(disabledConfig.required_env || []).join(', ') || disabledConfig.reason}
              </div>
            </div>
          )}
        </aside>

        <main>
          {config?.enabled ? (
            <OnlyOfficeHost config={config} />
          ) : (
            <div
              style={{
                minHeight: 420,
                border: '1px dashed var(--border-primary)',
                borderRadius: 8,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                color: 'var(--text-tertiary)',
                padding: 24,
                textAlign: 'center',
              }}
            >
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
