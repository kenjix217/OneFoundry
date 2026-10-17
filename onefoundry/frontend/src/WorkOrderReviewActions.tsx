import { useState } from 'react';

import { listWorkOrders, reviewWorkOrder, type WorkOrder } from './workApi';

type Props = { projectId: string; order: WorkOrder; onSaved: () => void };

export function WorkOrderReviewActions({ projectId, order, onSaved }: Props) {
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  if (order.status !== 'needs_review' && order.status !== 'done') return null;

  async function decide(action: 'accept' | 'return' | 'reopen') {
    const trimmed = reason.trim();
    if (!trimmed) { setError('Enter a reason for this decision.'); return; }
    setBusy(true); setError(null); setNotice(null);
    try {
      const saved = await reviewWorkOrder(projectId, order.id, action, trimmed, order.version);
      const canonical = (await listWorkOrders(projectId)).find((item) => item.id === order.id);
      const expected = action === 'accept' ? 'done' : 'ready';
      if (!canonical || canonical.version <= order.version || canonical.version !== saved.version || canonical.status !== expected) {
        throw new Error('The decision returned, but saved work-order state did not match. Refresh before retrying.');
      }
      setReason('');
      setNotice(action === 'accept' ? 'Accepted and recorded in Activity.' :
        action === 'return' ? 'Returned to Ready and recorded in Activity.' : 'Reopened and recorded in Activity.');
      onSaved();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not confirm the decision. Refresh saved state.');
    } finally { setBusy(false); }
  }

  return <section className="work-review-actions" aria-label="Work order review">
    <h3>{order.status === 'needs_review' ? 'Review completion' : 'Reopen completed work'}</h3>
    <p>Declared deliverable: {order.deliverable_kind.replaceAll('_', ' ')}.</p>
    {order.acceptance_checks.length > 0 && <ul>{order.acceptance_checks.map((check, index) =>
      <li key={`${index}-${check}`}>{check}</li>)}</ul>}
    <label className="field"><span className="field-label">Reason for your decision</span>
      <textarea value={reason} maxLength={2000} rows={2} disabled={busy}
        onChange={(event) => setReason(event.target.value)} /></label>
    {order.status === 'needs_review' ? <div className="form-actions">
      <button className="button button-primary" type="button" disabled={busy || !reason.trim()}
        onClick={() => { void decide('accept'); }}>Accept</button>
      <button className="button button-outline" type="button" disabled={busy || !reason.trim()}
        onClick={() => { void decide('return'); }}>Return to Ready</button>
    </div> : <button className="button button-outline" type="button" disabled={busy || !reason.trim()}
      onClick={() => { void decide('reopen'); }}>Reopen without evidence</button>}
    {error && <p className="form-error" role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
  </section>;
}
