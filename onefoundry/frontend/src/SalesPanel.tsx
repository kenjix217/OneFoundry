import { useEffect, useState } from 'react';

import { ApiError } from './api';
import { createSalesDraft, getSalesDraft, salesEligibility, type SalesDraft, type SalesEligibility } from './salesApi';

type Props = { projectId: string; runId: string; onChanged?: () => void };

export function SalesPanel({ projectId, runId, onChanged }: Props) {
  const [eligibility, setEligibility] = useState<SalesEligibility | null>(null);
  const [draft, setDraft] = useState<SalesDraft | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    void Promise.all([
      salesEligibility(projectId, runId, controller.signal),
      getSalesDraft(projectId, runId, controller.signal).catch((cause: unknown) => {
        if (cause instanceof ApiError && cause.status === 404) return null;
        throw cause;
      }),
    ]).then(([state, saved]) => { setEligibility(state); setDraft(saved); setLoading(false); })
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === 'AbortError') return;
        setError(cause instanceof Error ? cause.message : 'Could not read Sales state.');
        setLoading(false);
      });
    return () => controller.abort();
  }, [projectId, runId, reload]);

  async function create() {
    setSaving(true);
    setError(null);
    try {
      const created = await createSalesDraft(projectId, runId);
      const saved = await getSalesDraft(projectId, runId);
      if (saved.id !== created.id || saved.body_sha256 !== created.body_sha256) {
        throw new ApiError('Sales draft readback differs. Inspect Activity before another action.', null);
      }
      setDraft(saved);
      onChanged?.();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not create Sales draft.');
    } finally {
      setSaving(false);
    }
  }

  return <section className="panel" aria-labelledby="sales-stage-heading">
    <h3 id="sales-stage-heading">Synthetic Sales continuation</h3>
    <p className="quiet-note">Creates an offline draft from the completed GTM output. Delivery requires an exact operator decision and goes only to local Mailpit.</p>
    {loading && <p role="status">Loading Sales state…</p>}
    {error && <p role="alert">{error} <button type="button" onClick={() => setReload((value) => value + 1)}>Refresh saved state</button></p>}
    {!loading && !draft && eligibility?.eligible &&
      <button className="button button-primary" disabled={saving} type="button" onClick={() => { void create(); }}>
        {saving ? 'Saving…' : 'Create local Sales draft'}
      </button>}
    {!loading && !draft && !eligibility?.eligible && <p>{eligibility?.reason}</p>}
    {draft && <div>
      <p><strong>Recipient:</strong> {draft.recipient}</p>
      <p><strong>Subject:</strong> {draft.subject}</p>
      <p><strong>Delivery:</strong> {draft.delivery_status}</p>
      <p>Approval decisions are available in Inbox.</p>

      <pre className="approval-content" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{draft.body}</pre>
      {draft.delivery_status === 'unknown' && <p role="status">Local delivery outcome is unknown. Inspect Mailpit and Activity; this draft will not send again.</p>}
      {draft.delivery_status === 'delivered' && <p role="status">Matched local Mailpit readback at {draft.sent_at}.</p>}
      <button className="button button-outline" type="button" onClick={() => setReload((value) => value + 1)}>Refresh delivery state</button>
    </div>}
  </section>;
}
