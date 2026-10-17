import { useEffect, useState } from 'react';

import { getExternalCode, getExternalCodeArtifact, reconcileExternalCode,
  type ExternalCodeArtifact, type ExternalCodeArtifactPreview, type ExternalCodeDetail } from './externalCodeApi';
import { snapshotCodeStatus } from './snapshotCodeStatus';

type Props = { projectId: string; runId: string; onTimelineRefresh: () => void; onDetailChange: (detail: ExternalCodeDetail | null) => void };

function name(adapter: string): string { return adapter === 'codex_cli' ? 'Codex CLI' : adapter === 'claude_code' ? 'Claude Code' : 'Offline local source probe'; }
function message(error: unknown): string {
  return error instanceof Error ? error.message : 'Saved coding state is unavailable.';
}

export function ExternalCodePanel({ projectId, runId, onTimelineRefresh, onDetailChange }: Props) {
  const [detail, setDetail] = useState<ExternalCodeDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<ExternalCodeArtifactPreview | null>(null);
  const [previewId, setPreviewId] = useState<string | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setDetail(null);
    onDetailChange(null);
    setLoading(true);
    setError(null);
    setPreview(null);
    setPreviewId(null);
    void getExternalCode(projectId, runId, controller.signal).then((saved) => {
      if (controller.signal.aborted) return;
      setDetail(saved);
      onDetailChange(saved);
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
    try {
      await reconcileExternalCode(projectId, runId);
      const saved = await getExternalCode(projectId, runId);
      setDetail(saved);
      onDetailChange(saved);
      onTimelineRefresh();
    } catch (cause) {
      setUncertain(true);
      setError(`${message(cause)} Refresh saved state before checking again; the previous request may have completed.`);
    } finally { setBusy(false); }
  }

  async function showArtifact(artifact: ExternalCodeArtifact) {
    setPreview(null);
    setPreviewId(artifact.id);
    setError(null);
    try {
      const saved = await getExternalCodeArtifact(projectId, runId, artifact.id);
      if (saved.sha256 !== artifact.sha256 || saved.kind !== artifact.kind || saved.byte_size !== artifact.byte_size) {
        throw new Error('Saved artifact preview does not match its recorded hash and size.');
      }
      setPreview(saved);
    } catch (cause) { setError(message(cause)); }
  }

  const queued = detail?.attempts.some((attempt) => attempt.status === 'queued') ?? false;
  const offline = detail?.attempts.some((attempt) => attempt.adapter_id === 'offline_probe') ?? false;
  const snapshotFixture = detail?.attempts.some((attempt) => attempt.target_id === 'github_snapshot_codex_fixture_v1') ?? false;
  return <section className="code-panel" aria-labelledby="external-code-heading">
    <div className="section-heading"><div><p className="eyebrow">Execution Gateway</p><h3 id="external-code-heading">{snapshotFixture ? 'Managed synthetic Codex CLI run' : offline ? 'Offline local source probe' : 'External coding agent'}</h3></div></div>
    <p className="quiet-note">{snapshotFixture ? snapshotCodeStatus(detail) : offline ? 'The local source probe checks a saved repository revision in a disposable runner. No coding provider was called. A queued attempt awaits a saved runner receipt; this run does not merge, publish, or deploy changes.' : 'The selected coding CLI works in a disposable fixture repository. A queued attempt awaits a saved runner receipt. This run does not merge, publish, or deploy changes.'}</p>
    {loading && <p className="quiet-note" role="status">Loading saved coding attempt…</p>}
    {error && <p className="form-error" role="alert">{error}</p>}
    <button className="button button-outline" type="button" onClick={() => setReload((value) => value + 1)}>Refresh saved state</button>
    {queued && <button className="button button-outline" type="button" disabled={busy || uncertain}
      onClick={() => { void reconcile(); }}>{busy ? 'Checking…' : 'Check runner receipt'}</button>}
    {detail?.attempts.map((attempt) => <article className="timeline-step" key={attempt.id}>
      <div className="timeline-step-body">
        <h4>{attempt.target_id === 'github_snapshot_codex_fixture_v1' ? 'Managed synthetic Codex CLI' : name(attempt.adapter_id)} · {attempt.status}</h4>
        <p>{attempt.adapter_id === 'offline_probe' ? 'Local source check · no coding provider session' : attempt.provider_session_id ? 'Provider session recorded' : 'Provider session not recorded'}</p>
        {attempt.adapter_id === 'offline_probe' && <p>Local source {attempt.source_relative_path ?? 'Saved repository'} · branch {attempt.source_branch}</p>}
        {attempt.target_id === 'github_snapshot_codex_fixture_v1' && <div aria-label="Bound fake GitHub snapshot">
          <p>Saved fake GitHub snapshot</p><p>Repository {attempt.snapshot_owner}/{attempt.snapshot_repo} · saved branch {attempt.snapshot_branch}</p>
          <p>Snapshot: {attempt.snapshot_byte_size} bytes · fixture source</p>
        </div>}
        <p>Reason: {attempt.reason_code?.replaceAll('_', ' ') ?? (attempt.status === 'queued' ? 'Awaiting runner receipt' : 'None recorded')}</p>
        <p>Branch {attempt.branch ?? 'Not recorded'} · {attempt.commit ? 'Changes recorded' : 'No saved changes'}</p>
        <p>{attempt.diff_sha256 ? 'Saved diff available' : 'No saved diff'} · {attempt.receipt_sha256 ? 'Runner result recorded' : 'No saved runner result'}</p>
        <p>{attempt.adapter_id === 'offline_probe' ? 'No model or coding provider usage' : attempt.target_id === 'github_snapshot_codex_fixture_v1' ? `Synthetic usage: ${attempt.tokens_used === null ? 'Not recorded' : `${attempt.tokens_used} fixture tokens`} · no live billing evidence` : `Usage: ${attempt.tokens_used === null ? 'Tokens unavailable' : `${attempt.tokens_used} tokens`} · ${attempt.cost_usd === null ? 'Cost unavailable' : `$${attempt.cost_usd.toFixed(4)}`}`}</p>
        <div className="code-tests"><h5>Reported tests</h5>
          {attempt.tests.length === 0 ? <p>No structured test results reported. Inspect the saved log if available.</p> :
            <ul>{attempt.tests.map((test, index) => <li key={`${test.name}-${index}`}>{test.name}: {test.status} · exit {test.exit_code}</li>)}</ul>}
        </div>
        {attempt.artifacts.length === 0 && <p>No saved artifacts.</p>}
        {attempt.artifacts.map((artifact) => <div key={artifact.id}>
          <p>{artifact.kind} · {artifact.byte_size} bytes</p>
          <button className="button button-outline" type="button" onClick={() => { void showArtifact(artifact); }}>Preview {artifact.kind}</button>
          {previewId === artifact.id && preview && <pre aria-label={`Verified ${attempt.target_id === 'github_snapshot_codex_fixture_v1' ? 'synthetic snapshot' : attempt.adapter_id === 'offline_probe' ? 'offline' : 'external'} ${artifact.kind}`}>{preview.text}</pre>}
        </div>)}
      </div>
    </article>)}
    {detail?.run.status === 'unknown' && <p className="quiet-note">The {snapshotFixture ? 'synthetic snapshot coding' : offline ? 'offline source probe' : 'coding'} outcome is unknown. Inspect saved state and runner evidence before any new launch.</p>}
  </section>;
}
