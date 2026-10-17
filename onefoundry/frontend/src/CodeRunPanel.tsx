import { useEffect, useState } from 'react';

import { ApiError } from './api';
import { createMergeProposal, getArtifactPreview, getSyntheticCode, reconcileSyntheticCode,
  type ArtifactPreview, type CodeArtifact, type CodeDetail, type CodeJob } from './codeApi';
import { listApprovals, type Approval } from './workApi';

type Props = { projectId: string; runId: string; onTimelineRefresh: () => void;
  onDetailChange?: (detail: CodeDetail | null) => void };

function message(error: unknown): string {
  return error instanceof Error ? error.message : 'Could not complete the request. Check saved state before retrying.';
}

function time(value: string | null): string {
  if (value === null) return 'Not recorded';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function stageName(job: CodeJob): string {
  return job.stage === 'qa' ? 'QA' : job.stage === 'merge' ? 'Local merge' : 'Engineer';
}

function statusName(job: CodeJob): string {
  return job.status === 'queued' ? 'Queued · awaiting runner receipt' : job.status;
}

function diffArtifact(detail: CodeDetail): CodeArtifact | undefined {
  return detail.jobs.flatMap((job) => job.artifacts)
    .find((artifact) => artifact.kind === 'diff');
}

export function CodeRunPanel({ projectId, runId, onTimelineRefresh, onDetailChange }: Props) {
  const [detail, setDetail] = useState<CodeDetail | null>(null);
  const [approval, setApproval] = useState<Approval | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<ArtifactPreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [reload, setReload] = useState(0);
  const [uncertain, setUncertain] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    setDetail(null);
    onDetailChange?.(null);
    setApproval(null);
    setPreview(null);
    void getSyntheticCode(projectId, runId, controller.signal).then(async (saved) => {
      const items = saved.synthetic_merge_approval_id ? await listApprovals(projectId, undefined, controller.signal) : [];
      if (controller.signal.aborted) return;
      const found = items.find((item) => item.id === saved.synthetic_merge_approval_id) ?? null;
      if (saved.synthetic_merge_approval_id && (!found || !found.synthetic_merge || found.run_id !== runId)) {
        throw new ApiError('The saved merge proposal could not be verified. Check the approval inbox.', null);
      }
      setDetail(saved);
      onDetailChange?.(saved);
      setApproval(found);
      setUncertain(false);
      setLoading(false);
    }).catch((cause: unknown) => {
      if (cause instanceof DOMException && cause.name === 'AbortError') return;
      setError(message(cause));
      setLoading(false);
    });
    return () => controller.abort();
  }, [projectId, runId, reload, onDetailChange]);

  async function reconcile() {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await reconcileSyntheticCode(projectId, runId);
      const saved = await getSyntheticCode(projectId, runId);
      if (saved.jobs.length === 0) throw new ApiError('Runner readback returned no jobs. Check saved state.', null);
      setNotice('Saved runner receipt checked. Queued jobs may still be awaiting a receipt.');
      setReload((value) => value + 1);
      onTimelineRefresh();
    } catch (cause) {
      setError(message(cause));
      setUncertain(true);
    } finally {
      setBusy(false);
    }
  }

  async function proposeMerge() {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const created = await createMergeProposal(projectId, runId);
      const [saved, approvals] = await Promise.all([getSyntheticCode(projectId, runId), listApprovals(projectId)]);
      const found = approvals.find((item) => item.id === created.id);
      if (saved.synthetic_merge_approval_id !== created.id || !found || found.status !== 'pending' || !found.synthetic_merge) {
        throw new ApiError('The merge proposal returned, but saved approval readback did not confirm it. Check saved state before retrying.', null);
      }
      setDetail(saved);
      onDetailChange?.(saved);
      setApproval(found);
      setNotice('Exact synthetic merge proposal is pending in the Approval inbox. Approval queues a local fixture merge.');
      onTimelineRefresh();
    } catch (cause) {
      setError(message(cause));
      setUncertain(true);
    } finally {
      setBusy(false);
    }
  }

  async function showDiff(saved: CodeArtifact) {
    setPreviewLoading(true);
    setError(null);
    try {
      setPreview(await getArtifactPreview(projectId, runId, saved));
    } catch (cause) {
      setError(message(cause));
    } finally {
      setPreviewLoading(false);
    }
  }

  const engineer = detail?.jobs.find((job) => job.stage === 'engineer');
  const qa = detail?.jobs.find((job) => job.stage === 'qa');
  const merge = detail?.jobs.find((job) => job.stage === 'merge');
  const diff = detail ? diffArtifact(detail) : undefined;
  const qaPassed = engineer?.status === 'done' && qa?.status === 'done';
  const awaitingReceipt = detail?.jobs.some((job) => job.status === 'queued') ?? false;

  return <section className="code-panel" aria-labelledby="code-panel-heading">
    <div className="section-heading"><div><p className="eyebrow">Controlled local fixture</p><h3 id="code-panel-heading">Engineer → QA runner</h3></div></div>
    <p className="quiet-note">Synthetic changes run in a disposable fixture repository. No model provider or external repository is used. Queued means the saved runner receipt has not been imported yet.</p>
    {notice && <p className="notice" role="status">{notice}</p>}
    {loading && <p className="quiet-note" role="status">Loading saved runner state…</p>}
    {error && <p className="form-error" role="alert">{error}</p>}
    {uncertain && !loading && <p className="quiet-note">Reload saved state before taking another action. No launch or proposal is retried automatically.</p>}
    {!loading && <button className="button button-outline" type="button" disabled={busy} onClick={() => { setReload((value) => value + 1); }}>Reload saved state</button>}
    {!loading && detail && <>
      <div className="code-summary">
        <div><span>Engineer</span><strong>{engineer ? statusName(engineer) : 'Pending'}</strong></div>
        <div><span>QA handoff</span><strong>{qa ? statusName(qa) : 'Pending'}</strong></div>
        <div><span>Merge</span><strong>{merge ? statusName(merge) : approval?.status === 'rejected' ? 'Rejected · no merge' : approval?.status === 'pending' ? 'Awaiting approval' : qaPassed ? 'Awaiting proposal' : 'Not ready'}</strong></div>
      </div>
      {awaitingReceipt && <div className="code-actions"><button className="button button-primary" type="button" disabled={busy || uncertain}
        onClick={() => { void reconcile(); }}>{busy ? 'Checking…' : 'Check runner receipt'}</button>
        <span className="quiet-note">This explicit check imports a receipt if one exists. It does not relaunch a job.</span></div>}
      {detail.jobs.length === 0 && <p className="empty-inline">No saved runner jobs were returned.</p>}
      <div className="code-jobs">{detail.jobs.map((job) => <article className="code-job" key={job.id}>
        <div className="detail-top"><h4>{stageName(job)}</h4><span className={`status-chip status-${job.status}`}>{statusName(job)}</span></div>
        {job.reason_code && <p className="code-reason">Reason: {job.reason_code}</p>}
        <div className="code-facts"><div><span>Branch</span><strong>{job.branch ?? 'Not recorded'}</strong></div>
          {job.source_job_id && <div><span>Handoff source job</span><strong>Previous work stage</strong></div>}
          {job.approval_id && <div><span>Merge approval</span><strong>Requested in Inbox</strong></div>}
          <div><span>Baseline commit</span><strong>{job.baseline_commit ? 'Saved' : 'Not recorded'}</strong></div>
          <div><span>Fixture commit</span><strong>{job.commit ? 'Saved' : 'Not recorded'}</strong></div>
          {job.stage === 'merge' && <div><span>Merged commit</span><strong>{job.merged_commit ? 'Saved' : 'Not recorded'}</strong></div>}
          {job.stage === 'merge' && <div><span>Observed target HEAD</span><strong>{job.observed_head_commit ? 'Checked' : 'Not observed'}</strong></div>}
          <div><span>Saved changes</span><strong>{job.diff_sha256 ? 'Recorded' : 'Not recorded'}</strong></div></div>
        <p className="quiet-note">Started {time(job.started_at)} · Finished {time(job.finished_at)}</p>
        {job.tests.length > 0 && <div className="code-tests"><h5>{job.stage === 'engineer' ? 'Before / after tests' : job.stage === 'merge' ? 'Post-merge test' : 'QA tests'}</h5>
          <ul>{job.tests.map((test) => <li key={test.name}><strong>{test.name}</strong><span className={`status-chip status-${test.status}`}>{test.status}</span><span>exit {test.exit_code}</span></li>)}</ul></div>}
        {job.artifacts.length > 0 && <div className="code-artifacts"><h5>Saved artifacts</h5><ul>{job.artifacts.map((artifact) =>
          <li key={artifact.id}><strong>{artifact.kind}</strong><span>Saved locally</span><small>{artifact.byte_size} bytes</small></li>)}</ul></div>}
      </article>)}</div>
      {diff && <div className="code-diff"><div className="detail-top"><h4>Saved diff</h4><button className="button button-outline" type="button" disabled={previewLoading}
        onClick={() => { void showDiff(diff); }}>{previewLoading ? 'Loading…' : 'View verified diff'}</button></div>

        {preview && <pre aria-label="Verified synthetic diff">{preview.text}</pre>}</div>}
      {qaPassed && !detail.synthetic_merge_approval_id && !merge && <div className="code-merge-step">
        <h4>QA passed · merge awaits approval</h4><p>Request an exact local fixture merge proposal. No merge is queued until the operator approves it.</p>
        <button className="button button-primary" type="button" disabled={busy || uncertain} onClick={() => { void proposeMerge(); }}>{busy ? 'Saving…' : 'Request synthetic merge approval'}</button>
      </div>}
      {approval && <div className="code-merge-step"><h4>Merge proposal · {approval.status}</h4>
        <p>{approval.status === 'pending' ? 'Review this exact proposal in the Approval inbox. Approval queues the local fixture merge; rejection never merges.' :
          approval.status === 'rejected' ? 'The proposal was rejected. No local merge was queued.' :
            merge?.status === 'done' ? 'Local fixture merge completed. Receipt is shown above.' :
              'Approved. Check the saved runner receipt to confirm the local merge outcome; do not submit the decision again.'}</p>
      </div>}
    </>}
  </section>;
}
