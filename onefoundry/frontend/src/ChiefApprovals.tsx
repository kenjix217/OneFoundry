import { useState } from 'react';
import { decideApproval, listApprovals, type Approval } from './workApi';
import { approvalPresentation } from './approvalPresentation';

export function ChiefApprovals({ projectId, approvals, onSaved }: { projectId: string; approvals: Approval[]; onSaved: () => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  async function decide(item: Approval, decision: 'approved' | 'rejected') {
    setBusy(item.id); setError(null);
    try {
      const result = await decideApproval(projectId, item.id, decision);
      const saved = (await listApprovals(projectId)).find((row) => row.id === item.id);
      if (result.status !== decision || saved?.status !== decision || saved.proposal_digest !== item.proposal_digest)
        throw new Error('The decision could not be confirmed. Refresh before retrying.');
      onSaved();
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Approval unavailable.'); }
    finally { setBusy(null); }
  }
  return <>{error && <p role="alert">{error}</p>}{approvals.map((item) => {
    const display = approvalPresentation(item);
    return <article className="chief-approval" key={item.id}><h3>Approval requested · {item.action_type.replaceAll('_', ' ')}</h3>
      <p>{display.target}</p><div className="chief-message-body">{display.content}</div>
      <details><summary>Exact proposal</summary><pre>{item.content}</pre></details>
      <button disabled={busy !== null} onClick={() => { void decide(item, 'approved'); }}>Approve</button>
      <button disabled={busy !== null} onClick={() => { void decide(item, 'rejected'); }}>Decline</button>
      <small>The decision is recorded for the action owner. Execution is tracked separately.</small>
    </article>;
  })}</>;
}
