import { useEffect, useState, type FormEvent } from 'react';

import { ApiError } from './api';
import { createCandidate, createResult, listCandidates, listResults,
  type Candidate, type MemoryScope, type SourceResult } from './memoryApi';
import { listRuns, type Run } from './workApi';

type Props = { projectId: string; reloadToken: number; onChanged: () => void };

function message(error: unknown): string {
  return error instanceof Error ? error.message : 'The request failed. Check the saved state before retrying.';
}

function time(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function ResultIntake({ projectId, reloadToken, onChanged }: Props) {
  const [runs, setRuns] = useState<Run[]>([]);
  const [results, setResults] = useState<SourceResult[]>([]);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [runId, setRunId] = useState('');
  const [summary, setSummary] = useState('');
  const [scope, setScope] = useState<MemoryScope>('project');
  const [saving, setSaving] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    void Promise.all([
      listRuns(projectId, '', controller.signal), listResults(projectId, controller.signal),
      listCandidates(projectId, controller.signal),
    ]).then(([runItems, resultItems, candidateItems]) => {
      setRuns(runItems);
      setResults(resultItems);
      setCandidates(candidateItems);
      setLoading(false);
    }).catch((cause: unknown) => {
      if (cause instanceof DOMException && cause.name === 'AbortError') return;
      setError(message(cause));
      setLoading(false);
    });
    return () => controller.abort();
  }, [projectId, reload, reloadToken]);

  async function saveCandidate(source: SourceResult): Promise<Candidate[]> {
    const created = await createCandidate(projectId, source.id);
    const items = await listCandidates(projectId);
    if (!items.some((item) => item.id === created.id && item.source_result_id === source.id && item.status === 'pending')) {
      throw new ApiError('Review could not be confirmed. Refresh saved results before trying again.', null);
    }
    return items;
  }

  async function submitResult(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    setActionError(null);
    setNotice(null);
    let resultSaved = false;
    try {
      const created = await createResult(projectId, runId, { summary: summary.trim(), scope });
      const items = await listResults(projectId);
      if (!items.some((item) => item.id === created.id && item.run_id === runId)) {
        throw new ApiError('The result returned, but source readback did not find it. Check the saved state before retrying.', null);
      }
      setResults(items);
      resultSaved = true;
      setRunId('');
      setSummary('');
      setCandidates(await saveCandidate(created));
      setNotice('Result saved and sent for review. Approve it in Browse & review before it becomes shared knowledge.');
      onChanged();
    } catch (cause) {
      setActionError(resultSaved ? `Your result is saved, but review could not be confirmed. ${message(cause)}` : message(cause));
      setReload((value) => value + 1);
    } finally {
      setSaving(false);
    }
  }

  async function stageCandidate(source: SourceResult) {
    setSaving(true);
    setActionError(null);
    setNotice(null);
    try {
      setCandidates(await saveCandidate(source));
      setNotice('Sent for memory review. Nothing is shared until you approve it.');
      onChanged();
    } catch (cause) {
      setActionError(message(cause));
    } finally {
      setSaving(false);
    }
  }

  const doneRuns = runs.filter((run) => run.status === 'done' && run.mode === 'manual_planning' &&
    !results.some((result) => result.run_id === run.id));

  return (
    <section className="memory-intake panel" aria-labelledby="memory-intake-heading">
      <div className="section-heading"><div><p className="eyebrow">Source intake</p><h2 id="memory-intake-heading">Completed-run results</h2>
        <p>Save a result from a completed manual run. It goes straight to memory review; you decide what to share.</p></div></div>
      {notice && <div className="notice" role="status">{notice}</div>}
      {loading && <div className="loading-panel" role="status"><span className="loading-orb" />Loading run sources…</div>}
      {error && !loading && <div className="empty-inline"><p role="alert">{error}</p><button className="button button-outline" type="button" onClick={() => setReload((value) => value + 1)}>Try again</button></div>}
      {!loading && !error && <>
        <form className="result-form" onSubmit={(event) => { void submitResult(event); }}>
          <label className="field"><span className="field-label">Completed run</span><select required value={runId} onChange={(event) => setRunId(event.target.value)}>
            <option value="">Choose a done run</option>{doneRuns.map((run) =>
              <option key={run.id} value={run.id}>{run.work_order_title} · {time(run.started_at)}</option>)}</select></label>
          {doneRuns.length === 0 && <p className="quiet-note">Finish a manual run with a done outcome before saving a result. Each run accepts one result.</p>}
          <label className="field"><span className="field-label">Result summary</span><textarea required maxLength={4096} rows={4} value={summary}
            onChange={(event) => setSummary(event.target.value)} placeholder="Enter the result observed by the operator" /></label>
          <label className="field"><span className="field-label">Memory scope</span><select value={scope} onChange={(event) => setScope(event.target.value as MemoryScope)}>
            <option value="project">This project only</option><option value="company">Company across projects</option></select></label>
          <p className="quiet-note">The summary is entered by the operator. No model generates or verifies it.</p>
          {actionError && <p className="form-error" role="alert">{actionError}</p>}
          <button className="button button-primary" type="submit" disabled={saving || doneRuns.length === 0}>{saving ? 'Saving…' : 'Save and send for review'}</button>
        </form>
        <div className="source-results"><h3>Source results <span>{results.length}</span></h3>
          {results.length === 0 && <div className="empty-inline">No completed-run results saved for this project.</div>}
          {results.map((source) => {
            const existing = candidates.find((candidate) => candidate.source_result_id === source.id);
            return <article key={source.id} className="source-card">
              <div className="detail-top"><span className={`status-chip ${source.scope === 'company' ? 'status-company' : 'status-soft'}`}>{source.scope}</span>
                <time dateTime={source.created_at}>{time(source.created_at)}</time></div>
              <p>{source.summary}</p>
              <div className="source-meta"><span>Run {runs.find((run) => run.id === source.run_id)?.work_order_title ?? 'Previous work order'}</span>
                <span>{source.source_kind === 'operator_entered' ? 'Operator entered' :
                  source.source_kind === 'synthetic_sales' ? 'Synthetic Sales' : 'Synthetic QA'}</span></div>
              {existing ? <span className={`status-chip status-${existing.status}`}>Candidate {existing.status}</span> :
                <button className="button button-outline" type="button" disabled={saving} onClick={() => { void stageCandidate(source); }}>Send for memory review</button>}
            </article>;
          })}</div>
      </>}
    </section>
  );
}
