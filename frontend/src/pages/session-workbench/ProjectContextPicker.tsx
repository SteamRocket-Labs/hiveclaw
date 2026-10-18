import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { projectsApi, type ProjectDetail, type ProjectScope, type ProjectSummary } from '../../api/domains/projects';

export interface SelectedProjectContext { type: 'project_ref'; project_id: string; entry_ids: string[] }

export function ProjectContextPicker({ scope, disabled, selection, onSelect }: {
  scope: ProjectScope; disabled: boolean; selection: SelectedProjectContext | null;
  onSelect: (selection: SelectedProjectContext | null) => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [next, setNext] = useState<number | null>(null);
  const [detail, setDetail] = useState<ProjectDetail | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const generation = useRef(0);
  const selected = useRef('');
  useEffect(() => () => { generation.current += 1; }, []);
  const list = async (offset = 0) => {
    const attempt = ++generation.current;
    setBusy(true); setError('');
    try {
      const result = await projectsApi.list(scope, offset);
      if (attempt !== generation.current) return;
      setProjects(old => offset ? [...old, ...result.projects] : result.projects); setNext(result.next_offset);
    } catch { if (attempt === generation.current) setError(t('projects.contextLoadFailed')); }
    finally { if (attempt === generation.current) setBusy(false); }
  };
  const read = async (id: string, offset = 0) => {
    const attempt = ++generation.current;
    selected.current = id;
    setBusy(true); setError('');
    if (!offset) { setDetail(null); onSelect(null); }
    if (!id) { setBusy(false); return; }
    try {
      const result = await projectsApi.read(scope, id, offset);
      if (attempt !== generation.current) return;
      setDetail(old => offset && old?.id === id ? { ...result, entries: [...old.entries, ...result.entries] } : result);
    } catch { if (attempt === generation.current) setError(t('projects.contextLoadFailed')); }
    finally { if (attempt === generation.current) setBusy(false); }
  };
  return <div className="session-project-picker">
    <button type="button" disabled={disabled || busy} aria-expanded={open} onClick={() => {
      setOpen(!open); if (!open) void list();
    }}>{t('projects.context')}</button>
    {selection && <span role="status">{t('projects.contextSelected', { count: selection.entry_ids.length })}</span>}
    {selection && <button type="button" disabled={disabled} onClick={() => onSelect(null)}>{t('projects.contextRemove')}</button>}
    {open && <fieldset disabled={disabled || busy}>
      <legend>{t('projects.contextChoose')}</legend>
      <p>{t('projects.contextHint')}</p>
      <select aria-label={t('projects.contextProject')} value={selected.current} onChange={e => void read(e.target.value)}>
        <option value="">{t('projects.contextProject')}</option>
        {projects.map(project => <option key={project.id} value={project.id}>{project.title}</option>)}
      </select>
      {next !== null && <button type="button" onClick={() => void list(next)}>{t('projects.loadMore')}</button>}
      {detail && <div className="session-project-references">
        {detail.entries.filter(entry => !entry.removed).map(entry => <label key={entry.id}>
          <input type="checkbox" disabled={entry.state !== 'linked'} checked={Boolean(selection?.project_id === detail.id && selection.entry_ids.includes(entry.id))}
            onChange={e => {
              const ids = selection?.project_id === detail.id ? selection.entry_ids : [];
              const entries = e.target.checked ? [...ids, entry.id] : ids.filter(id => id !== entry.id);
              onSelect(entries.length ? { type: 'project_ref', project_id: detail.id, entry_ids: entries } : null);
            }} />
          {entry.state === 'linked' ? entry.title : t('projects.contextSourceUnavailable')}
          {entry.section && <small>{entry.section}</small>}
        </label>)}
        {detail.next_offset !== null && <button type="button" onClick={() => void read(detail.id, detail.next_offset!)}>{t('projects.loadMore')}</button>}
      </div>}
    </fieldset>}
    {error && <p role="alert">{error} <button type="button" disabled={disabled || busy} onClick={() => {
      if (selected.current) void read(selected.current); else void list();
    }}>{t('projects.contextRetry')}</button></p>}
  </div>;
}
