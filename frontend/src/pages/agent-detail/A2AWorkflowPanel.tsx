import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { a2aWorkflowApi, type A2AGraphAction, type A2AGraphPreview } from '../../api/domains/a2aWorkflows';
import { chatApi } from '../../api/domains/chat';
import { fileApi } from '../../api/domains/files';
import { saveBlob } from '../../utils/authenticatedResource';

const EXAMPLE = JSON.stringify({
  name: 'Research → Review → Delivery',
  participants: { researcher: { agent_id: 'RESEARCHER_AGENT_UUID' }, writer: { agent_id: 'WRITER_AGENT_UUID' } },
  args_schema: { topic: { type: 'string', required: true } },
  nodes: [
    { id: 'research', type: 'agent_handoff_step', agent_ref: 'researcher', task: 'Research {{args.topic}} and save the report.',
      output_contract: { artifacts: [{ name: 'report', path: 'workspace/research.md' }] } },
    { id: 'review', type: 'gate_step', reason: 'Review the research before delivery.' },
    { id: 'delivery', type: 'agent_handoff_step', agent_ref: 'writer', task: 'Write the final report using the authorized research artifact.',
      input_artifacts: [{ from: 'research.report', as: 'research' }],
      output_contract: { artifacts: [{ name: 'final', path: 'workspace/final.md' }] } },
  ],
  edges: [{ from: 'research', to: 'review' }, { from: 'review', to: 'delivery' }],
}, null, 2);

export default function A2AWorkflowPanel({ agentId }: { agentId: string }) {
  const client = useQueryClient();
  const [sessionId, setSessionId] = useState('');
  const [definitionText, setDefinitionText] = useState(EXAMPLE);
  const [argsText, setArgsText] = useState('{"topic":""}');
  const [preview, setPreview] = useState<A2AGraphPreview | null>(null);
  const [intentId, setIntentId] = useState('');
  const [runId, setRunId] = useState('');
  const [error, setError] = useState('');
  const sessions = useQuery({ queryKey: ['a2a-workflow-sessions', agentId], queryFn: () => chatApi.listSessions(agentId) });
  const runs = useQuery({ queryKey: ['a2a-workflow-runs', agentId, sessionId],
    queryFn: () => a2aWorkflowApi.list(agentId, sessionId), enabled: !!sessionId });
  const run = useQuery({ queryKey: ['a2a-workflow-run', agentId, runId],
    queryFn: () => a2aWorkflowApi.read(agentId, runId), enabled: !!runId,
    refetchInterval: (query) => ['pending', 'running'].includes(query.state.data?.status ?? '') ? 5000 : false });
  const fail = (e: unknown) => setError(e instanceof Error ? e.message : String(e));
  const resetPreview = () => { setPreview(null); setIntentId(''); setError(''); };
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ['a2a-workflow-runs', agentId] });
    void client.invalidateQueries({ queryKey: ['a2a-workflow-run', agentId] });
  };
  const createSession = useMutation({ mutationFn: () => chatApi.createSession(agentId, 'A2A Workflow'),
    onSuccess: (session) => { setSessionId(session.id); setRunId(''); resetPreview();
      void client.invalidateQueries({ queryKey: ['a2a-workflow-sessions', agentId] }); }, onError: fail });
  const inspect = useMutation({ mutationFn: () => a2aWorkflowApi.preview(agentId, sessionId,
    JSON.parse(definitionText), JSON.parse(argsText)), onSuccess: (result) => {
      setPreview(result); setIntentId(crypto.randomUUID()); setError('');
    }, onError: fail });
  const start = useMutation({ mutationFn: () => a2aWorkflowApi.start(agentId, sessionId, intentId, preview!),
    onSuccess: (result) => { setRunId(result.run_id); setPreview(null); setError(''); refresh(); }, onError: fail });
  const control = useMutation({ mutationFn: ({ action, nodeId }: { action: A2AGraphAction; nodeId?: string }) =>
    a2aWorkflowApi.control(agentId, runId, action, nodeId), onSuccess: () => { setError(''); refresh(); }, onError: fail });

  return <section aria-label="A2A Workflow" className="agent-workflows-section">
    <p>Full-Agent process graph: each node has its own Session and tools. Only declared, verified artifacts cross an edge.</p>
    <label>Root Session
      <select aria-label="A2A root session" value={sessionId} disabled={inspect.isPending || start.isPending} onChange={(event) => {
        setSessionId(event.target.value); setRunId(''); resetPreview();
      }}>
        <option value="">Select your Session</option>
        {(sessions.data ?? []).filter((s) => !s.read_only).map((s) => <option key={s.id} value={s.id}>{s.title || s.id}</option>)}
      </select>
    </label>
    <button type="button" className="btn btn-ghost" disabled={createSession.isPending || inspect.isPending || start.isPending} onClick={() => createSession.mutate()}>New root Session</button>
    <p>Replace participant UUIDs with existing digital employees; the coordinating Agent is separate. This version supports dependency-ordered handoffs, human gates and UTF-8 text / JSON artifacts.</p>
    <label>Process definition (JSON)
      <textarea aria-label="A2A definition" rows={16} value={definitionText} disabled={inspect.isPending || start.isPending} onChange={(event) => {
        setDefinitionText(event.target.value); resetPreview();
      }} />
    </label>
    <label>Arguments (JSON)
      <textarea aria-label="A2A arguments" rows={3} value={argsText} disabled={inspect.isPending || start.isPending} onChange={(event) => {
        setArgsText(event.target.value); resetPreview();
      }} />
    </label>
    <div className="agent-workflows-actions">
      <button type="button" className="btn btn-secondary" disabled={!sessionId || inspect.isPending || start.isPending}
        onClick={() => { resetPreview(); inspect.mutate(); }}>Preview graph</button>
      <button type="button" className="btn btn-primary" disabled={!preview?.admissible || start.isPending}
        onClick={() => start.mutate()}>Start confirmed graph</button>
    </div>
    {preview && <div><p>{preview.node_order.join(' → ')}</p><code>{preview.definition_hash}</code>
      {preview.collaboration_checks.map((check) => <p key={check.node_id}>{check.node_id}: {check.allowed ? 'Allowed' : check.reason}</p>)}</div>}
    <label>Run history
      <select aria-label="A2A run history" value={runId} onChange={(event) => setRunId(event.target.value)}>
        <option value="">Select a run</option>
        {(runs.data ?? []).map((item) => <option value={item.run_id} key={item.run_id}>{item.name} — {item.status}</option>)}
      </select>
    </label>
    <button type="button" className="btn btn-ghost" onClick={refresh}>Refresh</button>
    {[error, sessions.error?.message, runs.error?.message, run.error?.message].filter(Boolean).map((message, i) =>
      <p role="alert" className="agent-workflows-error" key={i}>{message}</p>)}
    {run.data && <div>
      <p><strong>{run.data.status}</strong> {run.data.reason}</p>
      <code>{run.data.run_id}</code>
      <div className="agent-workflows-actions">
        <button type="button" className="btn btn-ghost" disabled={inspect.isPending || start.isPending} onClick={() => {
          setDefinitionText(JSON.stringify(run.data!.definition, null, 2));
          setArgsText(JSON.stringify(run.data!.args, null, 2)); resetPreview();
        }}>Reuse definition</button>
        {run.data.status === 'suspended' && <button type="button" className="btn btn-secondary" disabled={control.isPending}
          onClick={() => control.mutate({ action: 'resume' })}>Resume same run</button>}
        {!['completed', 'killed'].includes(run.data.status) && <button type="button" className="btn btn-danger" disabled={control.isPending}
          onClick={() => control.mutate({ action: 'cancel' })}>Cancel and reconcile children</button>}
      </div>
      <ol>{run.data.steps.map((step) => <li key={step.id}>
        <strong>{step.id}</strong> — {step.status} {step.error}
        {step.journal.session_id && <a href={`/agents/${step.journal.agent_id}/sessions/${step.journal.session_id}`}>Open Agent Session</a>}
        <p>Attempt {step.journal.attempt ?? 1}</p>
        {run.data!.status === 'suspended' && step.status === 'suspended' && <div className="agent-workflows-actions">
          {(step.type === 'gate_step' ? ['approve', 'reject'] as const : ['retry'] as const).map((action) =>
            <button type="button" className="btn btn-secondary" disabled={control.isPending} key={action}
              onClick={() => control.mutate({ action, nodeId: step.id })}>{action === 'retry' ? 'Retry node as new attempt' : action}</button>)}
        </div>}
        {(step.journal.artifacts ?? []).map((artifact) => <div key={artifact.artifact_id}>
          <button type="button" className="btn btn-ghost" onClick={() => {
            void fileApi.downloadArtifact(artifact.producer_agent_id, artifact.artifact_id)
              .then((blob) => saveBlob(blob, artifact.path.split('/').pop() || artifact.name)).catch(fail);
          }}>{artifact.path}</button>
          <code>{artifact.content_hash}</code>
        </div>)}
      </li>)}</ol>
    </div>}
  </section>;
}
