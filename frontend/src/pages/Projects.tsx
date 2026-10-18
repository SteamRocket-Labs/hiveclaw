import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { ApiError } from '../api/core';
import { agentApi } from '../api/domains/agents';
import { chatApi, type ChatSession } from '../api/domains/chat';
import { fileApi } from '../api/domains/files';
import { projectsApi, type ProjectDetail, type ProjectEntry, type ProjectScope, type ProjectSummary } from '../api/domains/projects';
import type { Agent } from '../types';
import type { FileItem, FileEditorState } from '../components/FileBrowser';
import { useAuthStore } from '../stores';
import AgentWorkspaceSection from './agent-detail/AgentWorkspaceSection';
import './Projects.css';

export default function Projects() {
  const user = useAuthStore(state => state.user);
  const { t } = useTranslation();
  const { projectId } = useParams();
  const [selectedTenant, setSelectedTenant] = useState(() => localStorage.getItem('current_tenant_id') || '');
  useEffect(() => {
    const update = () => setSelectedTenant(localStorage.getItem('current_tenant_id') || '');
    update(); window.addEventListener('storage', update);
    return () => window.removeEventListener('storage', update);
  }, [user?.id]);
  const tenant = user?.role === 'platform_admin' ? selectedTenant : user?.tenant_id;
  if (!user || !tenant) return <p role="status">{t('projects.selectCompany')}</p>;
  return <ProjectWorkbench key={`${user.id}:${user.role}:${tenant}`} scope={{ userId: user.id, tenantId: tenant }} projectId={projectId} />;
}

export function ProjectWorkbench({ scope, projectId }: { scope: ProjectScope; projectId?: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const mounted = useRef(true);
  const pending = useRef(false);
  const listGeneration = useRef(0);
  const detailGeneration = useRef(0);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [nextProject, setNextProject] = useState<number | null>(null);
  const [archived, setArchived] = useState(false);
  const [detail, setDetail] = useState<ProjectDetail | null>(null);
  const [removed, setRemoved] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [unknown, setUnknown] = useState(false);
  const [conflict, setConflict] = useState(false);
  const retry = useRef<(() => Promise<unknown>) | null>(null);
  const [title, setTitle] = useState('');
  const createId = useRef(crypto.randomUUID());
  const [rename, setRename] = useState('');
  const renameDirty = useRef(false);
  const [sections, setSections] = useState<Record<string, string>>({});
  const [email, setEmail] = useState('');
  const [ownerEmail, setOwnerEmail] = useState('');
  const [role, setRole] = useState<'reader' | 'editor'>('reader');
  const [opened, setOpened] = useState<string | null>(null);
  const openedRef = useRef(opened);
  openedRef.current = opened;
  const [openEntry, setOpenEntry] = useState<ProjectEntry | null>(null);
  const [editor, setEditor] = useState<FileEditorState | null>(null);
  const blocked = Boolean(editor?.dirty || editor?.busy || editor?.unavailable);
  const selected = useRef(projectId);
  selected.current = projectId;
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  const list = useCallback(async (offset = 0) => {
    const generation = ++listGeneration.current;
    try {
      const result = await projectsApi.list(scope, offset, archived);
      if (!mounted.current || listGeneration.current !== generation) return;
      setProjects(old => offset ? [...old, ...result.projects] : result.projects); setNextProject(result.next_offset);
    } catch (err) { if (mounted.current && listGeneration.current === generation) throw err; }
  }, [scope.userId, scope.tenantId, archived]);
  const load = useCallback(async (offset = 0, id = projectId) => {
    const generation = ++detailGeneration.current;
    if (!id) { setDetail(null); return; }
    try {
      const result = await projectsApi.read(scope, id, offset, removed);
      if (!mounted.current || selected.current !== id || detailGeneration.current !== generation) return;
      const sourceId = openedRef.current;
      let observed = sourceId ? result.entries.find(row => row.id === sourceId) : undefined;
      if (sourceId && !observed) {
        try { observed = await projectsApi.readEntry(scope, id, sourceId); }
        catch (err) { if (!(err instanceof ApiError) || err.status !== 404) throw err; }
      }
      if (!mounted.current || selected.current !== id || detailGeneration.current !== generation) return;
      setDetail(old => offset && old?.id === id ? { ...result, entries: [...old.entries, ...result.entries] } : result);
      if (sourceId && openedRef.current === sourceId) {
        if (observed?.state === 'linked' && !observed.removed) setOpenEntry(observed);
        else { setOpenEntry(null); setOpened(null); setEditor(null); }
      }
      if (!offset && !renameDirty.current) setRename(result.title);
      setUnknown(false); setConflict(false);
    } catch (err) {
      if (!mounted.current || selected.current !== id || detailGeneration.current !== generation) return;
      if (err instanceof ApiError && [403, 404].includes(err.status)) {
        setDetail(null); setOpened(null); setOpenEntry(null); setEditor(null); setProjects(old => old.filter(row => row.id !== id));
        setRename(''); renameDirty.current = false; setSections({});
      }
      throw err;
    }
  }, [scope.userId, scope.tenantId, projectId, removed]);
  const fail = (err: unknown) => {
    if (!mounted.current) return;
    const code = err instanceof ApiError ? (err.data as { code?: string } | null)?.code : undefined;
    setError(err instanceof ApiError && err.status === 403 ? t('projects.actionDenied')
      : code === 'project_recipient_unavailable' ? t('projects.recipientUnavailable')
        : err instanceof Error ? err.message : t('projects.loadFailed'));
  };
  useEffect(() => { void list().catch(fail); }, [list]);
  useEffect(() => {
    setDetail(null); setOpened(null); setOpenEntry(null); setEditor(null); setError(''); setNotice(''); setUnknown(false);
    setConflict(false); setRename(''); renameDirty.current = false; setSections({});
  }, [projectId]);
  useEffect(() => { void load().catch(fail); }, [load]);

  const refresh = async () => {
    setError('');
    try {
      await list();
      if (projectId) await load();
      else if (unknown) {
        try {
          const saved = await projectsApi.read(scope, createId.current);
          if (mounted.current) { setUnknown(false); navigate(`/projects/${saved.id}`); }
        } catch (err) {
          if (!(err instanceof ApiError) || err.status !== 404) throw err;
        }
      }
    } catch (err) { fail(err); }
  };
  const mutate = async (operation: () => Promise<unknown>) => {
    if (pending.current) return;
    pending.current = true; setBusy(true); setError(''); setNotice(''); retry.current = operation;
    try {
      await operation();
      if (!mounted.current) return;
      setUnknown(false); retry.current = null;
      setNotice(t('projects.recorded'));
      try {
        await list();
        if (selected.current) await load(0, selected.current);
      } catch (readError) { fail(readError); }
    } catch (err) {
      if (!mounted.current) return;
      const outcome = err instanceof ApiError ? (err.data as { outcome?: string } | null)?.outcome : undefined;
      setUnknown(outcome === 'unknown' || !(err instanceof ApiError)
        || outcome !== 'not_applied' && (err.status === 0 || err.status === 408 || err.status >= 500));
      if (err instanceof ApiError && err.status === 409) setConflict(true);
      fail(err);
      if (err instanceof ApiError && err.status === 403 && selected.current) {
        try { await load(0, selected.current); } catch (readError) { fail(readError); }
      }
    } finally { pending.current = false; if (mounted.current) setBusy(false); }
  };
  const open = detail && openEntry?.id === opened ? openEntry : null;
  const writing = busy || unknown || conflict;
  const entryStatus = (entry: ProjectEntry) => entry.state === 'denied' ? t('projects.sourceDenied')
    : entry.state === 'not_found' ? t('projects.sourceMissing') : t('projects.sourceReconcile');

  return <main className="project-workbench">
    <header className="page-header"><h1>{t('projects.title')}</h1><p>{t('projects.description')}</p>
      <button className="btn btn-secondary" onClick={() => void refresh()} disabled={busy}>{t('common.refresh')}</button>
    </header>
    {error && <p role="alert">{unknown ? t('projects.unknown') : conflict ? t('projects.conflict') : error}</p>}
    {notice && <p role="status">{notice}</p>}
    {unknown && retry.current && <button className="btn btn-secondary" disabled={busy}
      onClick={() => void mutate(retry.current!)}>{t('projects.retrySame')}</button>}
    <div className="project-workbench-grid">
      <aside className="card">
        <form onSubmit={event => {
          event.preventDefault(); const request = { id: createId.current, title: title.trim() };
          void mutate(async () => {
            const created = await projectsApi.create(scope, request);
            if (mounted.current) { createId.current = crypto.randomUUID(); setTitle(''); navigate(`/projects/${created.id}`); }
          });
        }}><label>{t('projects.newTitle')}<input className="form-input" value={title} disabled={writing}
            onChange={event => setTitle(event.target.value)} maxLength={200} required /></label>
          <button className="btn btn-primary" disabled={writing || !title.trim()}>{t('projects.create')}</button></form>
        <label><input type="checkbox" checked={archived} onChange={event => setArchived(event.target.checked)} />{t('projects.showArchived')}</label>
        <nav aria-label={t('projects.title')}>{projects.map(row => <Link key={row.id} to={`/projects/${row.id}`}
          aria-current={row.id === projectId ? 'page' : undefined} aria-disabled={blocked || writing}
          onClick={event => { if (blocked || writing) event.preventDefault(); }}>{row.title}{row.archived ? ` · ${t('projects.archived')}` : ''}</Link>)}</nav>
        {nextProject !== null && <button className="btn btn-secondary" onClick={() => void list(nextProject).catch(fail)}>{t('projects.loadMore')}</button>}
      </aside>
      <section className="card">
        {!detail ? <p role="status">{error ? t('projects.loadFailed') : t(projectId ? 'projects.loading' : 'projects.choose')}</p> : <>
          <h2>{detail.title}</h2>
          {detail.archived && <p role="status">{t('projects.archived')}</p>}
          {(detail.can_write || renameDirty.current) && <form onSubmit={event => {
            event.preventDefault(); const request = { expected_revision: detail.revision, title: rename.trim() };
            void mutate(async () => { await projectsApi.edit(scope, detail.id, request); renameDirty.current = false; });
          }}><label>{t('projects.rename')}<input className="form-input" value={rename} onChange={event => { renameDirty.current = true; setRename(event.target.value); }}
                disabled={writing} maxLength={200} required /></label>
            {!detail.can_write && <p role="status">{t('projects.writeDenied')}</p>}
            <button className="btn btn-secondary" disabled={writing || !detail.can_write || !rename.trim()}>{t('common.save')}</button></form>}
          {detail.can_manage && <button className="btn btn-secondary" disabled={writing}
            onClick={() => void mutate(() => projectsApi.edit(scope, detail.id, { expected_revision: detail.revision, archived: !detail.archived }))}>
            {detail.archived ? t('projects.unarchive') : t('projects.archive')}</button>}
          <label><input type="checkbox" checked={removed} onChange={event => setRemoved(event.target.checked)} />{t('projects.showRemoved')}</label>
          {blocked && <p role="status">{t('projects.finishEditing')}</p>}
          <ul className="project-workbench-entries">{detail.entries.map(entry => <li key={entry.id}>
            <span>{entry.section}</span><strong>{entry.state === 'linked' ? entry.title : entryStatus(entry)}</strong>
            {entry.removed ? <span>{t('projects.removed')}</span> : entry.state === 'linked' && (entry.target_kind === 'session'
              ? <Link to={`/agents/${entry.agent_id}/sessions/${entry.session_id}`} onClick={event => { if (blocked) event.preventDefault(); }}>{t('projects.openWork')}</Link>
              : <button className="btn btn-secondary" disabled={blocked && opened !== entry.id}
                onClick={() => { if (opened !== entry.id) { setOpened(entry.id); setOpenEntry(entry); setEditor(null); } }}>{t('projects.openFile')}</button>)}
            {(detail.can_write || sections[entry.id] !== undefined) && <>
              <form onSubmit={event => {
                event.preventDefault(); const section = (sections[entry.id] ?? entry.section).trim();
                void mutate(async () => {
                  await projectsApi.editEntry(scope, detail.id, entry.id, { expected_revision: detail.revision, section });
                  setSections(old => { const next = { ...old }; delete next[entry.id]; return next; });
                });
              }}><input className="form-input" aria-label={t('projects.section')} value={sections[entry.id] ?? entry.section}
                  disabled={writing} onChange={event => setSections(old => ({ ...old, [entry.id]: event.target.value }))} maxLength={100} />
                <button className="btn btn-secondary" disabled={writing || !detail.can_write || sections[entry.id] === undefined || sections[entry.id] === entry.section}>{t('projects.applySection')}</button></form>
              {detail.can_write && <button className="btn btn-secondary" disabled={writing || opened === entry.id && blocked}
                onClick={() => void mutate(() => projectsApi.editEntry(scope, detail.id, entry.id, {
                  expected_revision: detail.revision, removed: !entry.removed }))}>{entry.removed ? t('projects.restoreLink') : t('projects.removeLink')}</button>}
            </>}
          </li>)}</ul>
          {detail.next_offset !== null && <button className="btn btn-secondary" onClick={() => void load(detail.next_offset!).catch(fail)}>{t('projects.loadMore')}</button>}
          {detail.can_write && <ProjectSourcePicker scope={scope} disabled={writing} onAdd={input => {
            const request = { ...input, id: crypto.randomUUID(), expected_revision: detail.revision };
            void mutate(() => projectsApi.add(scope, detail.id, request));
          }} />}
          {detail.can_manage && <details><summary>{t('projects.sharing')}</summary><p>{t('projects.sharingScope')}</p>
            <form onSubmit={event => {
              event.preventDefault(); const request = { id: crypto.randomUUID(), expected_revision: detail.revision, email: email.trim(), role };
              void mutate(() => projectsApi.grant(scope, detail.id, request));
            }}><label>{t('projects.email')}<input className="form-input" type="email" value={email} onChange={event => setEmail(event.target.value)} disabled={writing} required /></label>
              <select className="form-select" aria-label={t('projects.access')} value={role} onChange={event => setRole(event.target.value as 'reader' | 'editor')} disabled={writing}>
                <option value="reader">{t('projects.reader')}</option><option value="editor">{t('projects.editor')}</option></select>
              <button className="btn btn-secondary" disabled={writing || detail.archived}>{t('projects.share')}</button></form>
            <ul>{detail.grants?.map(grant => <li key={grant.id}>{grant.recipient_label || t('projects.recipientUnavailable')} · {grant.actions.includes('write') ? t('projects.editor') : t('projects.reader')}
              <button className="btn btn-secondary" disabled={writing} onClick={() => void mutate(() => projectsApi.revoke(scope, detail.id, grant.id, detail.revision))}>{t('projects.revoke')}</button></li>)}</ul>
            <form onSubmit={event => {
              event.preventDefault(); const request = { expected_revision: detail.revision, owner_email: ownerEmail.trim() };
              void mutate(() => projectsApi.edit(scope, detail.id, request));
            }}><label>{t('projects.newOwner')}<input className="form-input" type="email" value={ownerEmail} disabled={writing}
                onChange={event => setOwnerEmail(event.target.value)} required /></label>
              <button className="btn btn-secondary" disabled={writing}>{t('projects.transfer')}</button></form>
          </details>}
        </>}
      </section>
    </div>
    {open?.agent_id && open.path && <section className="card" aria-label={t('projects.currentFile')}>
      <h2>{open.title}</h2><AgentWorkspaceSection key={`${open.agent_id}:${open.path}`} agentId={open.agent_id} singleFile={open.path}
        principalScopeKey={`${scope.userId}:${scope.tenantId}`} onEditorStateChange={setEditor} />
    </section>}
  </main>;
}

function ProjectSourcePicker({ scope, disabled, onAdd }: { scope: ProjectScope; disabled: boolean;
  onAdd: (input: { target_kind: 'workspace_file' | 'session'; agent_id: string; path?: string; session_id?: string; section: string }) => void }) {
  const { t } = useTranslation();
  const [agents, setAgents] = useState<Agent[]>([]);
  const [agent, setAgent] = useState('');
  const [kind, setKind] = useState<'workspace_file' | 'session'>('workspace_file');
  const [path, setPath] = useState('workspace');
  const [files, setFiles] = useState<FileItem[]>([]);
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [section, setSection] = useState('');
  const [error, setError] = useState('');
  const [sourceRevision, setSourceRevision] = useState(0);
  useEffect(() => {
    let active = true;
    void agentApi.list(scope.tenantId).then(rows => { if (active) setAgents(rows.filter(row => row.access_level !== 'operator')); })
      .catch(err => { if (active) setError(String(err)); });
    return () => { active = false; };
  }, [scope.tenantId]);
  useEffect(() => {
    let active = true; setFiles([]); setSessions([]); setError('');
    if (agent) void (kind === 'workspace_file' ? fileApi.list(agent, path).then(rows => { if (active) setFiles(rows); })
      : chatApi.listSessions(agent).then(rows => { if (active) setSessions(rows); }))
      .catch(err => { if (active) setError(String(err)); });
    return () => { active = false; };
  }, [agent, path, kind, sourceRevision]);
  return <section className="project-source-picker"><h3>{t('projects.addSource')}</h3>
    <label>{t('projects.employee')}<select className="form-select" value={agent} disabled={disabled}
      onChange={event => { setAgent(event.target.value); setPath('workspace'); }}><option value="">{t('projects.chooseEmployee')}</option>
      {agents.map(row => <option key={row.id} value={row.id}>{row.name}</option>)}</select></label>
    <select className="form-select" aria-label={t('projects.sourceKind')} value={kind} disabled={disabled} onChange={event => setKind(event.target.value as typeof kind)}>
      <option value="workspace_file">{t('projects.files')}</option><option value="session">{t('projects.works')}</option></select>
    <label>{t('projects.section')}<input className="form-input" value={section} disabled={disabled} maxLength={100} onChange={event => setSection(event.target.value)} /></label>
    {error && <p role="alert">{error}</p>}
    {agent && <button className="btn btn-secondary" disabled={disabled} onClick={() => setSourceRevision(value => value + 1)}>{t('projects.reloadSources')}</button>}
    {kind === 'workspace_file' ? <>
      <p>{path}</p>{path !== 'workspace' && <button className="btn btn-secondary" disabled={disabled} onClick={() => setPath(path.slice(0, path.lastIndexOf('/')))}>{t('projects.parentFolder')}</button>}
      <ul>{files.map(file => <li key={file.path}>{file.name} <button className="btn btn-secondary" disabled={disabled} onClick={() => {
        if (file.is_dir) setPath(file.path);
        else onAdd({ target_kind: kind, agent_id: agent, path: file.path, section: section.trim() });
      }}>{file.is_dir ? t('projects.openFolder') : t('projects.linkFile')}</button></li>)}</ul>
    </> : <ul>{sessions.map(session => <li key={session.id}>{session.title} <button className="btn btn-secondary" disabled={disabled}
      onClick={() => onAdd({ target_kind: kind, agent_id: agent, session_id: session.id, section: section.trim() })}>{t('projects.linkWork')}</button></li>)}</ul>}
  </section>;
}
