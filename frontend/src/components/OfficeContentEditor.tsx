import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ApiError } from '../api/core';
import type { OfficeContentChanges, OfficeContentModel, OfficeContentReceipt } from '../api/domains/office';
import type { FileEditorState } from './FileBrowser';
import './OfficeContentEditor.css';

export interface OfficeContentApi {
  read: (path: string) => Promise<OfficeContentModel>;
  write?: (path: string, changes: OfficeContentChanges) => Promise<OfficeContentReceipt>;
  preview: (path: string) => Promise<Blob>;
}
const valuesOf = (model: OfficeContentModel) => Object.fromEntries(model.fields.map(field => [field.node_path, field.value]));

export default function OfficeContentEditor({ path, api, readOnly, onEditorStateChange, onSaved }: {
  path: string;
  api: OfficeContentApi;
  readOnly: boolean;
  onEditorStateChange?: (state: FileEditorState) => void;
  onSaved?: () => void;
}) {
  const { t } = useTranslation();
  const [previewing, setPreviewing] = useState(true);
  const [previewUrl, setPreviewUrl] = useState('');
  const [previewError, setPreviewError] = useState(false);
  const [previewRevision, setPreviewRevision] = useState(0);
  const [model, setModel] = useState<OfficeContentModel | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const draftRef = useRef(drafts);
  const [current, setCurrent] = useState<OfficeContentModel | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [conflict, setConflict] = useState(false);
  const [unknown, setUnknown] = useState(false);
  const [savedHash, setSavedHash] = useState<string | null>(null);
  const [savedIssues, setSavedIssues] = useState<number | null>(null);
  const [denied, setDenied] = useState(false);
  const [writeDenied, setWriteDenied] = useState(false);
  const mounted = useRef(true);
  const pending = useRef(false);
  const purgeDenied = useCallback((err: unknown) => {
    if (!(err instanceof ApiError) || ![403, 404].includes(err.status)) return false;
    setDenied(true); setModel(null); setDrafts({}); draftRef.current = {}; setCurrent(null); setSavedHash(null);
    setConflict(false); setUnknown(false); setPreviewUrl('');
    return true;
  }, []);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const updateDrafts = (next: Record<string, string>) => { draftRef.current = next; setDrafts(next); };
  const dirtyPaths = model?.fields.filter(field => drafts[field.node_path] !== field.value).map(field => field.node_path) || [];
  const orphanPaths = model ? Object.keys(drafts).filter(key => !model.fields.some(field => field.node_path === key)) : [];
  const dirty = dirtyPaths.length + orphanPaths.length > 0;
  useEffect(() => {
    onEditorStateChange?.({ path, dirty, busy, unavailable: denied || Boolean(error) || conflict || unknown, lastSavedHash: savedHash });
  }, [path, dirty, busy, error, conflict, unknown, denied, savedHash, onEditorStateChange]);

  useEffect(() => {
    if (!previewing || denied) return;
    let active = true;
    let url = '';
    setPreviewUrl(''); setPreviewError(false);
    void api.preview(path).then(blob => {
      if (!active) return;
      url = URL.createObjectURL(blob); setPreviewUrl(url);
    }).catch(err => { if (active) { purgeDenied(err); setPreviewError(true); } });
    return () => { active = false; if (url) URL.revokeObjectURL(url); };
  }, [api, path, previewing, previewRevision, denied, purgeDenied]);

  const load = useCallback(async (compare = false) => {
    if (pending.current) return;
    pending.current = true; setBusy(true); setError('');
    try {
      const next = await api.read(path);
      if (!mounted.current) return;
      setDenied(false);
      setWriteDenied(Boolean(next.read_only));
      if (compare) setCurrent(next);
      else { setModel(next); updateDrafts(valuesOf(next)); setConflict(false); setUnknown(false); }
    } catch (err) { if (mounted.current) { purgeDenied(err); setError(err instanceof Error ? err.message : t('agent.workspace.office.loadFailed')); } }
    finally { pending.current = false; if (mounted.current) setBusy(false); }
  }, [api, path, t, purgeDenied]);

  const save = async () => {
    if (!model || !api.write || pending.current || conflict || unknown || !dirty || orphanPaths.length
      || readOnly || writeDenied || model.read_only) return;
    const submitted = { ...draftRef.current };
    const changes = model.fields.filter(field => submitted[field.node_path] !== field.value)
      .map(field => ({ node_path: field.node_path, value: submitted[field.node_path] }));
    pending.current = true; setBusy(true); setError('');
    try {
      const receipt = await api.write(path, { expected_current_hash: model.content_hash, changes });
      if (!mounted.current) return;
      setSavedHash(receipt.content_hash); setSavedIssues(receipt.validation_issue_count ?? null); setPreviewRevision(value => value + 1);
      const baseline = { ...model, content_hash: receipt.content_hash,
        fields: model.fields.map(field => ({ ...field, value: submitted[field.node_path] })) };
      setModel(baseline);
      onSaved?.();
      try {
        const observed = await api.read(path);
        if (!mounted.current) return;
        const newer = Object.fromEntries(Object.entries(draftRef.current).filter(([key, value]) => value !== submitted[key]));
        setModel(observed); updateDrafts({ ...valuesOf(observed), ...newer });
        setWriteDenied(Boolean(observed.read_only));
      } catch (err) { if (mounted.current) { purgeDenied(err); setError(err instanceof Error ? err.message : t('agent.workspace.office.loadFailed')); } }
    } catch (err) {
      if (!mounted.current) return;
      if (err instanceof ApiError && err.status === 403) {
        setWriteDenied(true); setConflict(true);
        try {
          const observed = await api.read(path);
          if (mounted.current) { setCurrent(observed); setError(t('agent.workspace.office.writeDenied')); }
        } catch (readError) {
          if (mounted.current) {
            purgeDenied(readError);
            setError(t('agent.workspace.office.accessChanged'));
          }
        }
        return;
      }
      if (purgeDenied(err)) { setError(t('agent.workspace.office.accessChanged')); return; }
      const outcome = err instanceof ApiError ? (err.data as { outcome?: string } | null)?.outcome : undefined;
      if (err instanceof ApiError && err.status === 409) setConflict(true);
      else if (outcome === 'unknown' || !(err instanceof ApiError)
        || outcome !== 'not_applied' && (err.status === 0 || err.status === 408 || err.status >= 500)) {
        setConflict(true); setUnknown(true);
      }
      setError(err instanceof Error ? err.message : t('agent.workspace.office.saveFailed'));
    } finally { pending.current = false; if (mounted.current) setBusy(false); }
  };
  const rebase = (discard: boolean) => {
    if (!current) return;
    const next = valuesOf(current);
    if (!discard) for (const key of dirtyPaths) next[key] = draftRef.current[key];
    setSavedHash(current.content_hash);
    setWriteDenied(current.read_only ?? writeDenied);
    setModel(current); updateDrafts(next); setCurrent(null); setConflict(false); setUnknown(false); setError('');
  };
  const missing = current && dirtyPaths.some(key => !current.fields.some(field => field.node_path === key));
  const fieldLabel = (index: number) => model?.kind === 'xlsx' ? model.fields[index].node_path.slice(1)
    : model?.kind === 'pptx' && model.fields[index].name ? model.fields[index].name
      : t('agent.workspace.office.textBlock', { index: index + 1 });

  return <div className="office-content-editor">
    <div className="office-content-toolbar">
      <button className="btn btn-secondary" aria-pressed={previewing} onClick={() => setPreviewing(true)}>{t('agent.workspace.office.preview')}</button>
      {!readOnly && api.write && <button className="btn btn-secondary" aria-pressed={!previewing}
        onClick={() => { setPreviewing(false); if (!model) void load(); }}>{t('agent.workspace.office.edit')}</button>}
    </div>
    {previewing ? previewUrl ? <iframe title={path} src={previewUrl} sandbox="" className="office-content-preview" />
      : <p role={previewError ? 'alert' : 'status'}>{previewError ? t('agent.workspace.office.previewFailed') : t('common.loading')}</p>
      : <>
        {error && <p role="alert">{unknown ? t('agent.workspace.fileSave.unknown') : conflict ? t('agent.workspace.fileSave.conflict') : error}</p>}
        {(conflict || error) && <button className="btn btn-secondary" disabled={busy} onClick={() => void load(Boolean(model))}>{t('agent.workspace.fileSave.loadCurrent')}</button>}
        {current && <section className="office-content-current" aria-label={t('agent.workspace.fileSave.currentVersion')}>
          <h4>{t('agent.workspace.fileSave.currentVersion')}</h4>
          {current.fields.map(field => <pre key={field.node_path}>{field.name || field.node_path}\n{field.value}</pre>)}
          {missing && <p role="alert">{t('agent.workspace.office.fieldsRemoved')}</p>}
          <button className="btn btn-secondary" disabled={busy} onClick={() => rebase(true)}>{t('agent.workspace.office.discard')}</button>
          <button className="btn btn-secondary" disabled={busy || Boolean(missing)} onClick={() => rebase(false)}>{t('agent.workspace.office.keepEdits')}</button>
        </section>}
        {busy && !model && <p role="status">{t('common.loading')}</p>}
        {model && <form onSubmit={event => { event.preventDefault(); void save(); }}>
          <p>{t('agent.workspace.office.contentScope')}</p>
          {(writeDenied || model.read_only) && <p role="status">{t('agent.workspace.office.writeDenied')}</p>}
          {model.fields.map((field, index) => <label key={field.node_path} className="office-content-field">
            <span>{fieldLabel(index)}</span>
            <textarea className="form-textarea" value={drafts[field.node_path] ?? field.value}
              onChange={event => updateDrafts({ ...draftRef.current, [field.node_path]: event.target.value })} />
          </label>)}
          {orphanPaths.map(key => <section key={key}>
            <label className="office-content-field"><span>{t('agent.workspace.office.retainedEdit')}</span>
              <textarea className="form-textarea" value={drafts[key]} readOnly /></label>
            <button type="button" className="btn btn-secondary" onClick={() => {
              const next = { ...draftRef.current }; delete next[key]; updateDrafts(next);
            }}>{t('agent.workspace.office.discardRetained')}</button>
          </section>)}
          {!model.fields.length && <p>{t('agent.workspace.office.noFields')}</p>}
          <button className="btn btn-primary" type="submit" disabled={busy || !dirty || Boolean(orphanPaths.length)
            || conflict || unknown || readOnly || writeDenied || model.read_only}>{t(busy ? 'agent.soul.saving' : 'common.save')}</button>
          {savedHash && !dirty && <span role="status">{savedIssues
            ? t('agent.workspace.office.savedWithIssues', { count: savedIssues }) : t('agent.workspace.fileSave.saved')}</span>}
        </form>}
      </>}
  </div>;
}
