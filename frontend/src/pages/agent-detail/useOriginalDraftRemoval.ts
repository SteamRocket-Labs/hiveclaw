import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react';

import { chatApi, type UploadedChatFile } from '../../api/domains/chat';

interface Draft { originalAttachment?: UploadedChatFile['original_attachment'] }
function authentication() {
  try { return JSON.stringify([localStorage.getItem('token'), localStorage.getItem('current_tenant_id')]); }
  catch { return null; }
}

export function useOriginalDraftRemoval<T extends Draft>({ files, setFiles, identity, onError }: {
  files: T[];
  setFiles: Dispatch<SetStateAction<T[]>>;
  identity: string;
  onError: (error: unknown) => void;
}) {
  const generation = useRef({ identity, epoch: 0 });
  if (generation.current.identity !== identity) generation.current = { identity, epoch: generation.current.epoch + 1 };
  const alive = useRef(true);
  const inflight = useRef<number | null>(null);
  const [pending, setPending] = useState<number | null>(null);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  async function remove(index: number) {
    const file = files[index];
    const epoch = generation.current.epoch;
    if (!file || inflight.current === epoch) return;
    const auth = authentication();
    const current = () => alive.current && generation.current.epoch === epoch && authentication() === auth;
    inflight.current = epoch; setPending(epoch);
    try {
      if (file.originalAttachment) {
        const result = await chatApi.releaseOriginalUploadDraft(file.originalAttachment.id, file.originalAttachment.content_sha256);
        if (!['released', 'retained', 'already_absent'].includes(result.status)) throw new Error('original_draft_release_unconfirmed');
      }
      if (current()) setFiles(previous => previous.filter(value => value !== file));
    } catch (error) { if (current()) onError(error); }
    finally {
      if (inflight.current === epoch) inflight.current = null;
      if (alive.current) setPending(value => value === epoch ? null : value);
    }
  }
  return { remove, removing: pending === generation.current.epoch };
}
