import { useEffect, useState, type FormEvent } from 'react';

import { getProjectPlan, updateProjectPlan, type ProjectPlan } from './projectPlanApi';

export function ProjectPlanPanel({ projectId }: { projectId: string }) {
  const [plan, setPlan] = useState<ProjectPlan | null>(null);
  const [draft, setDraft] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setPlan(null); setDraft(''); setError(null); setNotice(null);
    void getProjectPlan(projectId, controller.signal).then((saved) => {
      if (!controller.signal.aborted) { setPlan(saved); setDraft(saved.body); }
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : 'Project plan unavailable.');
    });
    return () => controller.abort();
  }, [projectId]);

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!plan || !draft.trim()) return;
    setBusy(true); setError(null); setNotice(null);
    try {
      const written = await updateProjectPlan(projectId, draft.trim(), plan.version);
      const confirmed = await getProjectPlan(projectId);
      if (confirmed.version !== written.version || confirmed.body !== written.body)
        throw new Error('The saved project plan changed during readback. Review it before editing again.');
      setPlan(confirmed); setDraft(confirmed.body);
      setNotice(`Project plan saved (version ${confirmed.version}).`);
    } catch (failure) {
      try {
        const current = await getProjectPlan(projectId);
        setPlan(current);
        if (current.body === draft.trim()) {
          setDraft(current.body);
          setNotice(`Current saved plan matches your text (version ${current.version}).`);
          return;
        }
      } catch { /* Keep the original failure. */ }
      setError(failure instanceof Error ? failure.message : 'Project plan could not be confirmed.');
    } finally { setBusy(false); }
  }

  return <section className="task-chat-brief" aria-label="Project plan">
    <h2>Project plan</h2>
    {!plan && !error && <p role="status">Loading saved project plan…</p>}
    {error && <p role="alert">{error}</p>}
    {error && plan && draft.trim() !== plan.body && <details><summary>Current saved plan · version {plan.version}</summary>
      <p>{plan.body || 'No project plan is saved.'}</p></details>}
    {notice && <p role="status">{notice}</p>}
    {plan && <form onSubmit={(event) => { void save(event); }}>
      <label className="field"><span className="field-label">Saved project-wide plan · version {plan.version}</span>
        <textarea rows={8} maxLength={4000} value={draft} disabled={busy}
          onChange={(event) => setDraft(event.target.value)} placeholder="Describe the steps and handoffs for this project." /></label>
      <button className="button button-outline" type="submit" disabled={busy || !draft.trim() || draft.trim() === plan.body}>
        {busy ? 'Saving…' : 'Save project plan'}
      </button>
    </form>}
  </section>;
}
