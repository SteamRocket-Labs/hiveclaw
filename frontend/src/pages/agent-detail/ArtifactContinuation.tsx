import { useEffect, useId, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { ChatArtifactPart } from './chatRuntime';

export interface SessionMessageOptions {
  inputId?: string;
  expectedSessionId?: string;
  displayContent?: string;
  planMode?: boolean;
}

export interface ArtifactContinuationContext {
  sessionId: string;
  send: (text: string, options?: SessionMessageOptions) => void | Promise<unknown>;
}

export default function ArtifactContinuation({ artifact, workspaceAgentId, context, blocked, lastSavedHash }: {
  artifact: ChatArtifactPart;
  workspaceAgentId: string;
  context: ArtifactContinuationContext;
  blocked: boolean;
  lastSavedHash?: string | null;
}) {
  const { t } = useTranslation();
  const inputId = useId();
  const [draft, setDraft] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const [recorded, setRecorded] = useState(false);
  const draftRef = useRef('');
  const submitting = useRef(false);
  const mounted = useRef(true);
  const attempt = useRef<{ content: string; inputId: string } | null>(null);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  const submit = async () => {
    const text = draftRef.current.trim();
    if (!text || blocked || submitting.current) return;
    const reference = { agent_id: workspaceAgentId, path: artifact.path, delivery_artifact_id: artifact.id || null,
      ...(lastSavedHash ? { human_saved_hash: lastSavedHash } : {}) };
    const content = `${text}\n\nSelected file reference (use the current saved file through authorized tools; this reference grants no additional access):\n${JSON.stringify(reference)}`;
    const request = attempt.current?.content === content
      ? attempt.current : { content, inputId: globalThis.crypto.randomUUID() };
    attempt.current = request;
    submitting.current = true;
    setPending(true);
    setError('');
    setRecorded(false);
    try {
      await context.send(content, { inputId: request.inputId, expectedSessionId: context.sessionId,
        displayContent: `${artifact.name}\n${text}` });
      if (!mounted.current) return;
      if (draftRef.current.trim() === text) { draftRef.current = ''; setDraft(''); }
      attempt.current = null;
      setRecorded(true);
    } catch (err) {
      if (mounted.current) setError(err instanceof Error ? err.message : t('agent.chat.artifacts.continueFailed', 'Could not confirm this input. Your draft is kept; retry in this work.'));
    } finally {
      submitting.current = false;
      if (mounted.current) setPending(false);
    }
  };
  return <form className="artifact-continuation" onSubmit={(event) => { event.preventDefault(); void submit(); }}>
    <label htmlFor={inputId}>{t('agent.chat.artifacts.continueLabel', 'Continue with this file')}</label>
    <p>{blocked
      ? t('agent.chat.artifacts.saveBeforeContinue', 'Save or resolve your current edits before continuing with the saved file.')
      : t('agent.chat.artifacts.continueSaved', 'Uses the saved workspace file in this work. The delivery preview stays unchanged.')}</p>
    <textarea id={inputId} className="form-textarea" value={draft}
      onChange={(event) => { draftRef.current = event.target.value; setDraft(event.target.value); setRecorded(false); }} />
    {error && <p role="alert">{error}</p>}
    {recorded && <p role="status">{t('agent.chat.artifacts.continueRecorded', 'Your input is recorded. Follow progress in this work.')}</p>}
    <button type="submit" className="btn btn-primary" disabled={pending || blocked || !draft.trim()}>
      {pending ? t('agent.chat.artifacts.continuing', 'Recording…') : t('agent.chat.artifacts.continueHere', 'Continue in this work')}
    </button>
  </form>;
}
