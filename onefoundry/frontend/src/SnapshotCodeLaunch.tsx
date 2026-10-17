import { useEffect, useState } from 'react';

import { ApiError } from './api';
import { getExternalCode, type ExternalCodeAttempt } from './externalCodeApi';
import { getSnapshotCodeEligibility, launchSnapshotCode, type SavedFixtureSnapshot, type SnapshotCodeEligibility } from './snapshotCodeApi';
import { listRuns } from './workApi';
import { readSessionKey } from './brandStorage';

type Props = { projectId: string; orderId: string; onOpenRun: (runId: string) => void };

function storageKey(projectId: string, orderId: string, snapshotId: string): string {
  return `onefoundry.synthetic-snapshot-code.${projectId}.${orderId}.${snapshotId}`;
}

function rememberedKey(key: string): string | null {
  try { return readSessionKey(key); }
  catch { throw new ApiError('Browser session storage is unavailable. This launch requires a durable retry key.', null); }
}

function rememberKey(key: string, value: string): void {
  try { sessionStorage.setItem(key, value); }
  catch { throw new ApiError('Browser session storage is unavailable. Synthetic launch was not sent.', null); }
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Saved synthetic coding state is unavailable.';
}

function matchesSnapshot(attempt: ExternalCodeAttempt, snapshot: SavedFixtureSnapshot, key: string): boolean {
  return attempt.target_id === 'github_snapshot_codex_fixture_v1' &&
    attempt.idempotency_key === key && attempt.snapshot_attempt_id === snapshot.snapshot_attempt_id &&
    attempt.snapshot_source_id === snapshot.source_id && attempt.snapshot_account_id === snapshot.account_id &&
    attempt.snapshot_owner === snapshot.owner && attempt.snapshot_repo === snapshot.repo &&
    attempt.snapshot_branch === snapshot.branch && attempt.snapshot_revision === snapshot.saved_revision &&
    attempt.snapshot_tree === snapshot.source_tree && attempt.snapshot_path === snapshot.snapshot_path &&
    attempt.snapshot_sha256 === snapshot.snapshot_sha256 && attempt.snapshot_byte_size === snapshot.snapshot_byte_size &&
    attempt.snapshot_receipt_sha256 === snapshot.receipt_sha256;
}

export function SnapshotCodeLaunch({ projectId, orderId, onOpenRun }: Props) {
  const [eligibility, setEligibility] = useState<SnapshotCodeEligibility | null>(null);
  const [selectedId, setSelectedId] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(null); setEligibility(null);
    void getSnapshotCodeEligibility(projectId, orderId, controller.signal).then((result) => {
      if (controller.signal.aborted) return;
      setEligibility(result);
      setSelectedId((current) => result.snapshots.some((item) => item.snapshot_attempt_id === current) ? current : '');
      setLoading(false);
    }).catch((cause: unknown) => {
      if (cause instanceof DOMException && cause.name === 'AbortError') return;
      setError(errorMessage(cause)); setLoading(false);
    });
    return () => controller.abort();
  }, [projectId, orderId, refresh]);

  const selected = eligibility?.snapshots.find((item) => item.snapshot_attempt_id === selectedId);
  let priorKey: string | null = null;
  try { if (selected) priorKey = rememberedKey(storageKey(projectId, orderId, selectedId)); }
  catch { /* The action reports the storage error when clicked. */ }

  async function findSaved(key: string, snapshot: SavedFixtureSnapshot): Promise<string | null> {
    const runs = await listRuns(projectId);
    for (const run of runs.filter((item) => item.work_order_id === orderId && item.mode === 'external_code')) {
      const detail = await getExternalCode(projectId, run.id);
      const matchingIntent = detail.attempts.find((attempt) => attempt.target_id === 'github_snapshot_codex_fixture_v1' &&
        attempt.idempotency_key === key && attempt.snapshot_attempt_id === snapshot.snapshot_attempt_id);
      if (matchingIntent && !matchesSnapshot(matchingIntent, snapshot, key)) {
        throw new ApiError('Saved coding attempt does not match every selected snapshot identity field.', null);
      }
      if (matchingIntent) return run.id;
    }
    return null;
  }

  async function act(snapshot: SavedFixtureSnapshot) {
    setBusy(true); setError(null); setNotice(null);
    const keyName = storageKey(projectId, orderId, snapshot.snapshot_attempt_id);
    let sent = false;
    try {
      const existing = rememberedKey(keyName);
      const key = existing ?? crypto.randomUUID();
      if (!existing) {
        rememberKey(keyName, key);
        sent = true;
        const launched = await launchSnapshotCode(projectId, orderId, snapshot.snapshot_attempt_id, key);
        const attempt = launched.attempts.find((item) => item.target_id === 'github_snapshot_codex_fixture_v1' &&
          item.idempotency_key === key && item.snapshot_attempt_id === snapshot.snapshot_attempt_id);
        if (!attempt || !matchesSnapshot(attempt, snapshot, key)) {
          throw new ApiError('The launch response did not match the selected saved snapshot. Inspect saved state.', null);
        }
      }
      const runId = await findSaved(key, snapshot);
      if (runId) { onOpenRun(runId); return; }
      setError('The launch outcome is not confirmed by saved state. Keep this idempotency key and inspect Activity; do not start another launch.');
    } catch (cause) {
      if (sent) {
        try {
          const key = rememberedKey(keyName);
          const runId = key ? await findSaved(key, snapshot) : null;
          if (runId) { onOpenRun(runId); return; }
        } catch { /* Keep the original error and prevent a new launch. */ }
      }
      setError(`${errorMessage(cause)} ${sent ? 'Launch outcome may be unknown. Use Inspect saved launch; do not submit a new launch.' : 'No launch was sent.'}`);
    } finally { setBusy(false); }
  }

  return <details className="fixture-diagnostics">
    <summary>Diagnostics · managed synthetic Codex CLI run</summary>
    <p className="quiet-note">This path can run a pinned Codex CLI executable against a local fake TLS provider using a completed, approved synthetic v3 snapshot and a separate ready built-in Engineer work order. The fixed fake model ignores the Engineer profile's selected model. The GitHub revision is fixture-asserted. No live provider account, real GitHub fetch, or desktop session control is used.</p>
    {loading && <p role="status">Checking saved synthetic snapshot eligibility…</p>}
    {error && <p className="form-error" role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {!loading && <button className="button button-outline" type="button" disabled={busy} onClick={() => setRefresh((value) => value + 1)}>Refresh saved snapshots</button>}
    {eligibility?.reason && <p className="quiet-note">Synthetic snapshot coding unavailable: {eligibility.reason}</p>}
    {eligibility && eligibility.snapshots.length === 0 && <p className="quiet-note">No approved, completed, current fake snapshot is available. Create and approve one in Integrations and Approvals first.</p>}
    {eligibility && eligibility.snapshots.length > 0 && <label className="field"><span className="field-label">Approved fake GitHub snapshot</span>
      <select value={selectedId} disabled={busy} onChange={(event) => setSelectedId(event.target.value)}>
        <option value="">Choose an exact saved snapshot</option>
        {eligibility.snapshots.map((item, index) => <option key={item.snapshot_attempt_id} value={item.snapshot_attempt_id}>
          {item.owner}/{item.repo} · {item.branch} · Snapshot {index + 1}
        </option>)}
      </select></label>}
    {selected && <div className="quiet-note" aria-label="Selected synthetic snapshot binding">
      <p>Saved fixture revision selected.</p>

      <p>Saved snapshot: {selected.snapshot_byte_size} bytes</p>

      <p>The runner creates a separate local synthetic baseline commit. It is not the GitHub revision.</p>
    </div>}
    <button className="button button-outline" type="button" disabled={busy || !selected || (!priorKey && !eligibility?.eligible)}
      onClick={() => { if (selected) void act(selected); }}>{busy ? 'Checking saved launch…' : priorKey ? 'Inspect saved launch' : 'Run managed synthetic Codex CLI'}</button>
  </details>;
}
