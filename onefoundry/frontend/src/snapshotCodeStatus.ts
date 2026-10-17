import type { ExternalCodeDetail } from './externalCodeApi';

export function snapshotCodeStatus(detail: ExternalCodeDetail | null): string | null {
  const attempt = detail?.attempts.find((item) => item.target_id === 'github_snapshot_codex_fixture_v1');
  if (!attempt) return null;
  const boundary = 'The GitHub revision is fixture-asserted, and local baseline and candidate commits are separate. No live provider account, real GitHub fetch, desktop session control, merge, publish, or deployment is shown.';
  if (attempt.status === 'queued') {
    return `The synthetic launch is queued. A Codex CLI execution or final outcome is not yet confirmed by a saved runner receipt. ${boundary}`;
  }
  if (attempt.status === 'unknown') {
    return `The synthetic launch outcome is unknown. Inspect the saved attempt and runner evidence before another action; a Codex CLI result is not confirmed. ${boundary}`;
  }
  if (attempt.status === 'done' && attempt.receipt_sha256 && attempt.provider_session_id) {
    return `A saved runner receipt confirms the pinned Codex CLI executed against a local fake TLS provider. The fixed fake model ignores the Engineer profile's selected model. ${boundary}`;
  }
  if (attempt.status === 'failed') {
    return `The synthetic attempt is recorded as failed. Inspect its reason and any saved receipt to determine how far Codex CLI progressed. ${boundary}`;
  }
  return `The synthetic run is marked done, but a complete CLI receipt is unavailable in this readback. Inspect saved state before treating execution as confirmed. ${boundary}`;
}
