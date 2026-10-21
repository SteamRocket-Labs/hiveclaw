import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { ApiError } from '../../api/core';
import { enterpriseApi, type OriginalReference, type OriginalObservation, type OriginalOperationFamily, type OriginalOperationRequest,
  type OriginalPolicy, type OriginalRecord, type OriginalState } from '../../api/domains/enterprise';
import { useAuthStore } from '../../stores';
import Modal from '../../components/ui/Modal';
import OriginalReferenceList from './OriginalReferenceList';

interface SubmittedOperation {
  version: 1;
  actor: string;
  tenant: string;
  original: OriginalRecord;
  family: OriginalOperationFamily;
  request: OriginalOperationRequest;
}
const storageKey = (actor: string, tenant: string) => `hive.original-operation:${JSON.stringify([actor, tenant])}`;
const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const sha256 = /^[a-f0-9]{64}$/;
function readSubmitted(actor: string, tenant: string): SubmittedOperation | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(storageKey(actor, tenant)) || 'null');
    if (value?.version !== 1 || value.actor !== actor || value.tenant !== tenant || !value.original
      || !['reconciliation', 'retention', 'sweep', 'references'].includes(value.family)
      || !uuid.test(value.original.blob_id) || !uuid.test(value.request?.operation_id)
      || !sha256.test(value.request.expected_content_sha256) || value.original.content_sha256 !== value.request.expected_content_sha256) return null;
    const actions = { reconciliation: ['recover'], retention: ['quarantine', 'restore'], sweep: ['sweep'], references: ['retire_reference', 'withdraw_source'] };
    if (!actions[value.family as OriginalOperationFamily].includes(value.request.action)) return null;
    if (['retention', 'sweep'].includes(value.family) && (!Number.isInteger(value.request.expected_policy_version) || value.request.expected_policy_version < 0)) return null;
    if (value.family === 'references' && (!uuid.test(value.request.reference_id) || !sha256.test(value.request.expected_reference_sha256))) return null;
    if (value.family === 'sweep' && (!sha256.test(value.request.expected_manifest_sha256)
      || (value.request.retry_operation_id != null && !uuid.test(value.request.retry_operation_id)))) return null;
    return value;
  } catch { return null; }
}

export default function WorkspaceOriginalStorageSection({ selectedTenantId }: { selectedTenantId: string }) {
  const user = useAuthStore((state) => state.user);
  return user && (user.role === 'org_admin' || user.role === 'platform_admin')
    ? <OriginalOperations key={`${user.id}:${selectedTenantId}`} actor={user.id} tenant={selectedTenantId} /> : null;
}

function OriginalOperations({ actor, tenant }: { actor: string; tenant: string }) {
  const { t } = useTranslation();
  const alive = useRef(true);
  const inflight = useRef(false);
  const loadTicket = useRef(0);
  const policyTicket = useRef(0);
  const [policy, setPolicy] = useState<OriginalPolicy | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [days, setDays] = useState('');
  const [grace, setGrace] = useState('');
  const [policyError, setPolicyError] = useState(false);
  const [rows, setRows] = useState<OriginalRecord[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [filter, setFilter] = useState<OriginalState | ''>('');
  const [onlyUnreferenced, setOnlyUnreferenced] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [storageError, setStorageError] = useState(false);
  const [detail, setDetail] = useState<{ original: OriginalRecord; result: OriginalObservation } | null>(null);
  const [submitted, setSubmitted] = useState<SubmittedOperation | null>(() => readSubmitted(actor, tenant));
  const submittedRef = useRef(submitted);
  const [missingReceipt, setMissingReceipt] = useState(false);
  const [review, setReview] = useState<SubmittedOperation | null>(null);
  const [observed, setObserved] = useState<OriginalObservation | null>(null);
  const [referenceTarget, setReferenceTarget] = useState<OriginalRecord | null>(null);
  const [referenceVersion, setReferenceVersion] = useState(0);
  const cancelReview = useRef<HTMLButtonElement>(null);
  const active = () => alive.current;
  const states: Record<OriginalState, string> = { uploading: t('enterprise.originals.uploading'), available: t('enterprise.originals.available'),
    quarantined: t('enterprise.originals.quarantined'), deleting: t('enterprise.originals.deleting'), deleted: t('enterprise.originals.deleted'), failed: t('enterprise.originals.failed') };
  const outcomes: Record<string, string> = { present: t('enterprise.originals.present'), missing: t('enterprise.originals.missing'),
    absent: t('enterprise.originals.absent'), changed: t('enterprise.originals.changed'), unknown: t('enterprise.originals.unknown'),
    unavailable: t('enterprise.originals.unavailable'), denied: t('enterprise.originals.denied'), held: t('enterprise.originals.needsReview'),
    policy_disabled: t('enterprise.originals.policyDisabled'), referenced: t('enterprise.originals.referenced'),
    retention_age: t('enterprise.originals.retained'), grace_period: t('enterprise.originals.retained'),
    unresolved_state: t('enterprise.originals.reconcileNeeded'), quarantine_evidence_unavailable: t('enterprise.originals.reconcileNeeded'),
    explicit_inspected_retry_required: t('enterprise.originals.reconcileNeeded'), active_generation_exists: t('enterprise.originals.newGeneration'),
    provider_missing: t('enterprise.originals.missing'), provider_changed: t('enterprise.originals.changed'), provider_unknown: t('enterprise.originals.unknown') };

  function track(value: SubmittedOperation | null) {
    const completedId = submittedRef.current?.request.operation_id;
    submittedRef.current = value;
    if (active()) setSubmitted(value);
    try {
      if (value) sessionStorage.setItem(storageKey(actor, tenant), JSON.stringify(value));
      else if (readSubmitted(actor, tenant)?.request.operation_id === completedId) sessionStorage.removeItem(storageKey(actor, tenant));
    } catch { if (active()) setStorageError(true); }
  }

  async function loadPolicy() {
    const ticket = ++policyTicket.current;
    try {
      const value = await enterpriseApi.getOriginalPolicy(tenant);
      if (!active() || ticket !== policyTicket.current) return;
      setPolicy(value); setEnabled(value.enabled); setDays(String(value.retention_days)); setGrace(String(value.grace_days)); setPolicyError(false);
    } catch { if (active() && ticket === policyTicket.current) setPolicyError(true); }
  }
  async function loadCatalog(after?: string) {
    const ticket = ++loadTicket.current;
    try {
      const value = await enterpriseApi.listOriginals(tenant, { state: filter || undefined, unreferenced: onlyUnreferenced, after });
      if (!active() || ticket !== loadTicket.current) return;
      setRows((previous) => after ? [...previous, ...value.items] : value.items); setCursor(value.next_cursor); setError(null);
    } catch { if (active() && ticket === loadTicket.current) setError(t('enterprise.originals.loadFailed')); }
  }
  useEffect(() => {
    alive.current = true;
    void loadPolicy();
    return () => { alive.current = false; loadTicket.current += 1; policyTicket.current += 1; };
    // The keyed component owns exactly one authenticated actor/company.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    setRows([]); setCursor(null); setDetail(null); setReview(null);
    void loadCatalog();
    // Each filter change invalidates every earlier page response.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter, onlyUnreferenced]);

  async function guard(action: () => Promise<void>) {
    if (inflight.current) return;
    inflight.current = true; setBusy(true); setError(null);
    try { await action(); }
    catch { if (active()) setError(t('enterprise.originals.operationFailed')); }
    finally { inflight.current = false; if (active()) setBusy(false); }
  }
  function command(original: OriginalRecord, family: OriginalOperationFamily, action: OriginalOperationRequest['action'], snapshot?: OriginalObservation): SubmittedOperation {
    const request: OriginalOperationRequest = { operation_id: crypto.randomUUID(), expected_content_sha256: original.content_sha256, action };
    if (family === 'retention' || family === 'sweep') request.expected_policy_version = snapshot?.policy_version ?? policy?.version;
    if (family === 'sweep') request.expected_manifest_sha256 = snapshot?.manifest_sha256;
    return { version: 1, actor, tenant, original, family, request };
  }
  function validateReferenceReceipt(operation: SubmittedOperation, result: OriginalObservation) {
    if (operation.family === 'references' && (result.reference_id !== operation.request.reference_id
      || result.blob_id !== operation.original.blob_id || result.operation_id !== operation.request.operation_id)) {
      throw new Error('original_reference_receipt_mismatch');
    }
  }
  async function submit(operation: SubmittedOperation, remember: boolean) {
    if (remember && submittedRef.current && submittedRef.current.request.operation_id !== operation.request.operation_id) return;
    if (remember) { track(operation); if (active()) setMissingReceipt(false); }
    try {
      const result = await enterpriseApi.originalOperation(tenant, operation.original.blob_id, operation.family, operation.request);
      validateReferenceReceipt(operation, result);
      if (remember) track(null);
      if (!active()) return;
      setObserved(result); setMissingReceipt(false); setReview(null);
      if (operation.family === 'references') setReferenceVersion(value => value + 1);
      if (!remember && !result.replayed) setDetail({ original: operation.original, result });
      else setDetail(null);
      await loadCatalog();
    } catch (failure) {
      if (!active()) return;
      if (remember) setReview(null);
      setError(remember ? t('enterprise.originals.outcomeUnknown') : t('enterprise.originals.operationFailed'));
      if (failure instanceof ApiError && (failure.status === 403 || failure.status === 409)) setDetail(null);
    }
  }
  const inspect = (original: OriginalRecord) => guard(async () => {
    setDetail(null); setReview(null);
    await submit(command(original, original.state === 'uploading' ? 'reconciliation' : 'retention', 'inspect'), false);
  });
  const mutate = (family: OriginalOperationFamily, action: OriginalOperationRequest['action']) => guard(async () => {
    if (!detail || submittedRef.current) return;
    await submit(command(detail.original, family, action, detail.result), true);
  });
  async function checkSubmitted() {
    await guard(async () => {
      const operation = submittedRef.current;
      if (!operation) return;
      try {
        const receipt = await enterpriseApi.getOriginalOperation(tenant, operation.original.blob_id, operation.family, operation.request.operation_id);
        validateReferenceReceipt(operation, receipt.result);
        if (!active()) return;
        setObserved(receipt.result); setDetail(null); setReview(null); setMissingReceipt(false);
        if (operation.family === 'references') setReferenceVersion(value => value + 1);
        if (receipt.status !== 'running') track(null);
        await loadCatalog();
      } catch (failure) {
        if (!active()) return;
        setMissingReceipt(failure instanceof ApiError && failure.status === 404);
        setError(failure instanceof ApiError && failure.status === 404 ? t('enterprise.originals.notRecorded') : t('enterprise.originals.operationFailed'));
      }
    });
  }
  async function savePolicy() {
    await guard(async () => {
      if (!policy || policyError) return;
      policyTicket.current += 1;
      setDetail(null); setReview(null);
      try {
        const value = await enterpriseApi.updateOriginalPolicy(tenant, { enabled, retention_days: Number(days), grace_days: Number(grace), expected_version: policy.version });
        if (active()) { setPolicy(value); setEnabled(value.enabled); setDays(String(value.retention_days)); setGrace(String(value.grace_days)); }
      } catch { if (active()) setPolicyError(true); }
    });
  }
  function reviewSweep(retry = false) {
    if (!detail?.result.manifest_sha256 || submittedRef.current) return;
    const operation = command(detail.original, 'sweep', 'sweep', detail.result);
    if (retry) operation.request.retry_operation_id = rows.find((row) => row.blob_id === detail.original.blob_id)?.active_sweep?.operation_id;
    setReview(operation);
  }
  function reviewReference(reference: OriginalReference, withdrawal = false) {
    if (!referenceTarget || !(withdrawal ? reference.withdrawable : reference.retirable) || submittedRef.current || inflight.current) return;
    const operation = command(referenceTarget, 'references', withdrawal ? 'withdraw_source' : 'retire_reference');
    operation.request.reference_id = reference.reference_id;
    operation.request.expected_reference_sha256 = withdrawal ? reference.withdrawal_snapshot_sha256 : reference.snapshot_sha256;
    setReview(operation);
  }
  const numbersValid = [days, grace].every((value) => value.trim() !== '' && Number.isFinite(Number(value)) && Number(value) >= 0);
  const current = detail?.result;
  const currentRow = rows.find((row) => row.blob_id === detail?.original.blob_id);
  const canRetry = current?.state === 'deleting' && currentRow?.active_sweep?.status === 'partial'
    && currentRow.active_sweep.reconciled_outcome === 'present' && !currentRow.operation_conflict;
  const resumeRecord = submitted && rows.find((row) => row.blob_id === submitted.original.blob_id && row.content_sha256 === submitted.request.expected_content_sha256);
  const destructiveReview = review?.family === 'sweep' && review.request.action === 'sweep';
  const withdrawalReview = review?.family === 'references' && review.request.action === 'withdraw_source';
  const referenceReviewTitle = withdrawalReview ? t('enterprise.originals.dispositionReview', 'Review inactive-source disposition')
    : t('enterprise.originals.refReview', 'Review reference retirement');
  useEffect(() => { if (review) cancelReview.current?.focus(); }, [review]);

  return <section className="card ws-info-card" aria-label={t('enterprise.originals.title')}>
    <h4>{t('enterprise.originals.title')}</h4>
    <p className="ws-info-desc">{t('enterprise.originals.scope')}</p>
    {policy ? <fieldset disabled={busy}>
      <legend>{t('enterprise.originals.policy')}</legend>
      <p>{policy.source === 'unset' ? t('enterprise.originals.defaultDisabled') : t('enterprise.storage.version', { version: policy.version })}</p>
      <label><input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} /> {t('enterprise.originals.enabled')}</label>
      <label>{t('enterprise.originals.days')}<input className="form-input" type="number" min="0" step="any" value={days} onChange={(event) => setDays(event.target.value)} /></label>
      <label>{t('enterprise.originals.grace')}<input className="form-input" type="number" min="0" step="any" value={grace} onChange={(event) => setGrace(event.target.value)} /></label>
      <button className="btn btn-primary" disabled={!numbersValid || policyError} onClick={() => void savePolicy()}>{t('enterprise.originals.savePolicy')}</button>
    </fieldset> : null}
    {policyError ? <p role="alert">{t('enterprise.originals.policyUnknown')}</p> : null}
    <button className="btn" disabled={busy} onClick={() => void guard(async () => { setDetail(null); setReview(null); await Promise.all([loadPolicy(), loadCatalog()]); })}>{t('enterprise.originals.reload')}</button>
    {submitted ? <div role="status">
      <p>{t('enterprise.originals.pending', { id: submitted.request.operation_id })}</p>
      <button className="btn" disabled={busy} onClick={() => void checkSubmitted()}>{t('enterprise.originals.checkSubmitted')}</button>
      {missingReceipt && resumeRecord ? <button className="btn" disabled={busy} onClick={() => setReview({ ...submitted, original: resumeRecord })}>{t('enterprise.originals.resubmit')}</button> : null}
      <p className="ws-info-desc">{t('enterprise.originals.forgetHint')}</p>
      <button className="btn" disabled={busy} onClick={() => { track(null); setMissingReceipt(false); setReview(null); }}>{t('enterprise.originals.forget')}</button>
    </div> : null}
    {storageError ? <p role="status">{t('enterprise.originals.storageUnavailable')}</p> : null}
    <div className="ws-info-actions ws-info-original-controls">
      <label>{t('enterprise.originals.filter')}<select value={filter} disabled={busy} onChange={(event) => setFilter(event.target.value as OriginalState | '')}>
        <option value="">{t('enterprise.originals.allStates')}</option>
        {Object.entries(states).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </select></label>
      <label><input type="checkbox" checked={onlyUnreferenced} disabled={busy} onChange={(event) => setOnlyUnreferenced(event.target.checked)} />{t('enterprise.originals.unreferenced')}</label>
    </div>
    {rows.map((row) => <article className="card" key={row.blob_id}>
      <p><code>{row.blob_id}</code> · {states[row.state]} · {row.provider} · {row.size_bytes} B</p>
      <p>{t('enterprise.originals.references', { count: row.reference_count })}</p>
      <button className="btn" disabled={busy} onClick={() => setReferenceTarget(row)}>{t('enterprise.originals.manageReferences', 'Retained references')}</button>
      <button className="btn" disabled={busy || (row.state !== 'uploading' && (!policy || policyError))} onClick={() => void inspect(row)}>{t('enterprise.originals.inspect')}</button>
    </article>)}
    {!rows.length && !busy ? <p>{t('enterprise.originals.empty')}</p> : null}
    {cursor ? <button className="btn" disabled={busy} onClick={() => void guard(() => loadCatalog(cursor))}>{t('enterprise.originals.more')}</button> : null}
    {detail && current ? <div aria-label={t('enterprise.originals.inspection')}>
      <p className="ws-info-original-identity"><code>{detail.original.content_sha256}</code></p>
      <p>{states[current.state]} · {outcomes[current.observation || current.outcome || ''] || (current.hold_reason ? t('enterprise.originals.needsReview') : t('enterprise.originals.noHold'))}</p>
      {current.hold_reason ? <p role="status">{outcomes[current.hold_reason] || t('enterprise.originals.needsReview')}</p> : null}
      {current.delete_after ? <p>{t('enterprise.originals.deadline', { date: current.delete_after })}</p> : null}
      {current.state === 'uploading' && current.outcome === 'present' ? <button className="btn" disabled={busy || Boolean(submitted)} onClick={() => void mutate('reconciliation', 'recover')}>{t('enterprise.originals.recover')}</button> : null}
      {current.state === 'available' && !current.hold_reason ? <button className="btn" disabled={busy || Boolean(submitted)} onClick={() => void mutate('retention', 'quarantine')}>{t('enterprise.originals.quarantine')}</button> : null}
      {current.state === 'quarantined' ? <button className="btn" disabled={busy || Boolean(submitted)} onClick={() => void mutate('retention', 'restore')}>{t('enterprise.originals.restore')}</button> : null}
      {current.sweep_eligible ? <button className="btn ws-info-delete-btn" disabled={busy || Boolean(submitted)} onClick={() => reviewSweep()}>{t('enterprise.originals.remove')}</button> : null}
      {current.state === 'deleting' ? <button className="btn" disabled={busy} onClick={() => void guard(() => submit(command(detail.original, 'sweep', 'reconcile', current), false))}>{t('enterprise.originals.verifyDeletion')}</button> : null}
      {canRetry ? <button className="btn ws-info-delete-btn" disabled={busy || Boolean(submitted)} onClick={() => reviewSweep(true)}>{t('enterprise.originals.retryDeletion')}</button> : null}
    </div> : null}
    {referenceTarget ? <OriginalReferenceList key={`${tenant}:${referenceTarget.blob_id}`} tenant={tenant} blobId={referenceTarget.blob_id}
      version={referenceVersion} disabled={busy || Boolean(submitted)} onRetire={reviewReference} onWithdraw={reference => reviewReference(reference, true)} /> : null}
    <Modal open={Boolean(review)} title={review?.family === 'references' ? referenceReviewTitle : t('enterprise.originals.review')}
      ariaLabel={review?.family === 'references' ? referenceReviewTitle : t('enterprise.originals.review')}
      onClose={() => { if (!inflight.current) setReview(null); }}
      footer={<>
        <button ref={cancelReview} className="btn" disabled={busy} onClick={() => setReview(null)}>{t('common.cancel')}</button>
        <button className="btn ws-info-delete-btn" disabled={busy || !review} onClick={() => review && void guard(() => submit(review, true))}>{review?.family === 'references'
          ? withdrawalReview ? t('enterprise.originals.dispositionConfirm', 'Confirm source withdrawal') : t('enterprise.originals.refConfirm', 'Confirm reference retirement')
          : destructiveReview ? t('enterprise.originals.confirm') : t('enterprise.originals.confirmAction')}</button>
      </>}>
      {review ? <>
        <p className="ws-info-original-identity"><code>{review.original.blob_id}</code></p>
        <p className="ws-info-original-identity"><code>{review.request.expected_content_sha256}</code></p>
        <p>{review.original.provider} · {review.original.size_bytes} B</p>
        <p>{withdrawalReview ? t('enterprise.originals.dispositionWarning', 'Stop future use under administrator authority for this inactive uploader. Keep the historical uploader, original bytes, work references and retention holds. This does not delete the original or grant access to a successor.')
          : review.family === 'references' ? t('enterprise.originals.refWarning', 'Retire only this verified holding. The stored original remains subject to the separate retention and physical-cleanup policy.')
          : destructiveReview ? t('enterprise.originals.deleteWarning') : t('enterprise.originals.resubmitWarning')}</p>
      </> : null}
    </Modal>
    {observed ? <p role="status">{observed.reference_id ? observed.outcome === 'source_withdrawn'
      ? t('enterprise.originals.dispositionRecorded', 'Future source use stopped. The original, historical uploader and retention holds are preserved.')
      : observed.outcome === 'source_withdrawal_held' ? t('enterprise.originals.dispositionHeld', 'Source use was not changed. Refresh and verify the current uploader status.')
      : observed.outcome === 'reference_retired'
      ? t('enterprise.originals.refRecorded', 'Reference retirement recorded. The original has not been physically deleted.')
      : observed.outcome === 'reference_absent' ? t('enterprise.originals.refAbsent', 'This original no longer has that reference.')
      : t('enterprise.originals.refHeld', 'The reference was retained. Refresh and verify its current requirements.')
      : t('enterprise.originals.recorded', { id: observed.operation_id, state: states[observed.state], date: observed.observed_at })}</p> : null}
    {error ? <p role="alert">{error}</p> : null}
  </section>;
}
