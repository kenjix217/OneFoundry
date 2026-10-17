import { useEffect, useState } from 'react';

import { ApiError } from './api';
import { MemorySourceReview } from './MemorySourceReview';
import { listCandidates, reviewCandidate, type Candidate, type CandidateStatus } from './memoryApi';

type Props = { projectId: string; reloadToken: number; onChanged: () => void };

function time(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : 'Review failed. Check the saved state before retrying.';
}

export function CandidateReview({ projectId, reloadToken, onChanged }: Props) {
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [status, setStatus] = useState<CandidateStatus>('pending');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [savingId, setSavingId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    void listCandidates(projectId, controller.signal).then((items) => {
      setCandidates(items);
      setLoading(false);
    }).catch((cause: unknown) => {
      if (cause instanceof DOMException && cause.name === 'AbortError') return;
      setError(message(cause));
      setLoading(false);
    });
    return () => controller.abort();
  }, [projectId, reload, reloadToken]);

  async function decide(item: Candidate, decision: 'approved' | 'rejected') {
    setSavingId(item.id);
    setActionError(null);
    setNotice(null);
    try {
      await reviewCandidate(projectId, item.id, decision);
      const readback = await listCandidates(projectId);
      if (!readback.some((candidate) => candidate.id === item.id && candidate.status === decision)) {
        throw new ApiError('Review returned, but candidate readback did not confirm the decision. Check the saved state.', null);
      }
      setCandidates(readback);
      setNotice(decision === 'approved'
        ? 'Candidate approved. Search memory to inspect the published source-linked item.'
        : 'Candidate rejected. It was not published to memory.');
      onChanged();
    } catch (cause) {
      setActionError(message(cause));
    } finally {
      setSavingId(null);
    }
  }

  const shown = candidates.filter((candidate) => candidate.status === status);

  return (
    <section className="candidate-review panel" aria-labelledby="candidate-review-heading">
      <div className="section-heading"><div><p className="eyebrow">Ready for your review</p><h2 id="candidate-review-heading">Memory candidates</h2>
        <p>Review saved results before the team uses them. Approve to share as memory, or reject to keep them out.</p></div></div>
      <div className="inbox-tabs" role="group" aria-label="Candidate status">
        {(['pending', 'approved', 'rejected'] as CandidateStatus[]).map((value) =>
          <button key={value} type="button" aria-pressed={status === value} className={status === value ? 'active' : ''}
            onClick={() => { setStatus(value); setNotice(null); setActionError(null); }}>{value}</button>)}
      </div>
      {notice && <div className="notice" role="status">{notice}</div>}
      {actionError && <p className="form-error" role="alert">{actionError}</p>}
      {loading && <div className="loading-panel" role="status"><span className="loading-orb" />Loading candidates…</div>}
      {error && !loading && <div className="empty-inline"><p role="alert">{error}</p><button className="button button-outline" type="button" onClick={() => setReload((value) => value + 1)}>Try again</button></div>}
      {!loading && !error && shown.length === 0 && <div className="empty-inline">No {status} candidates for this project.</div>}
      {!loading && !error && shown.map((item) => <article key={item.id} className="candidate-card">
        <div className="detail-top"><span className={`status-chip ${item.scope === 'company' ? 'status-company' : 'status-soft'}`}>{item.scope} scope</span>
          <span className={`status-chip status-${item.status}`}>{item.status}</span></div>
        <p className="candidate-text">{item.summary}</p>
        <MemorySourceReview projectId={projectId} candidate={item} />
        <div className="source-meta"><span>Saved source result</span>
          <span>Proposed {time(item.created_at)}</span></div>
        {item.reviewed_at && <p className="quiet-note">Reviewed {time(item.reviewed_at)}</p>}
        {item.status === 'pending' && <div className="approval-actions"><button className="button button-outline" type="button" disabled={savingId !== null}
          onClick={() => { void decide(item, 'rejected'); }}>Reject candidate</button>
          <button className="button button-primary" type="button" disabled={savingId !== null}
            onClick={() => { void decide(item, 'approved'); }}>Approve to memory</button></div>}
      </article>)}
    </section>
  );
}
