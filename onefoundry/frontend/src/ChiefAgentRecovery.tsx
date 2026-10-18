import { useRef, useState } from 'react';
import { acknowledgeUnknown, reconcileTeamActions, type TeamSessionView } from './taskSessionApi';

export function ChiefAgentRecovery({ projectId, orderId, view, onChanged }: {
  projectId: string; orderId: string; view: TeamSessionView; onChanged: () => Promise<TeamSessionView>;
}) {
  const [busy, setBusy] = useState(false);
  const [reason, setReason] = useState('');
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checkedAttemptId, setCheckedAttemptId] = useState<string | null>(null);
  const actionId = useRef<string | null>(null);
  const active = view.attempts.some((item) => item.status === 'queued' || item.status === 'running');
  const started = view.tool_calls.some((item) => item.status === 'started');
  const unknown = view.attempts.find((item) => item.status === 'unknown' && !item.unknown_acknowledged && !item.terminal_result_reconciled);
  if (!started && !unknown) return null;
  async function reconcile() {
    if (busy || active) return;
    setBusy(true); setError(null); setNotice(null);
    try {
      const count = await reconcileTeamActions(projectId, orderId, view.session.id);
      const saved = await onChanged();
      setCheckedAttemptId(unknown && saved.attempts.some((item) => item.id === unknown.id && item.status === 'unknown' &&
        !item.unknown_acknowledged && !item.terminal_result_reconciled) ? unknown.id : null);
      setNotice(`${count} saved action receipts reconciled. No agent turn was launched.`);
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Saved actions could not be reconciled.'); }
    finally { setBusy(false); }
  }
  async function abandon() {
    if (busy || active || started || !unknown || checkedAttemptId !== unknown.id || reason.trim().length < 10) return;
    setBusy(true); setError(null); setNotice(null);
    try {
      actionId.current ??= crypto.randomUUID();
      await acknowledgeUnknown(projectId, orderId, view.session.id, unknown.id, actionId.current, reason.trim());
      await onChanged();
      actionId.current = null; setReason('');
      setNotice('Unverified outcome abandoned. Its history stays unknown; no turn was launched.');
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'The decision could not be confirmed.'); }
    finally { setBusy(false); }
  }
  return <section className="chief-agent-recovery" aria-label="Agent recovery">
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {(started || unknown) && !active && <button type="button" disabled={busy} onClick={() => { void reconcile(); }}>Reconcile saved actions</button>}
    {unknown && <fieldset disabled={busy || active || started || checkedAttemptId !== unknown.id}>
      <legend>Unknown outcome needs your decision</legend>
      <p>Check the saved actions first. Abandoning this unverified result preserves its unknown history. It does not confirm success or replay the attempt.</p>
      <label>Reason for abandoning the unverified result
        <textarea value={reason} maxLength={1000} onChange={(event) => { setReason(event.target.value); actionId.current = null; }} /></label>
      <button type="button" disabled={reason.trim().length < 10} onClick={() => { void abandon(); }}>Abandon unverified result</button>
    </fieldset>}
  </section>;
}
