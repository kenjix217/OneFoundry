import { useEffect, useState } from 'react';

import { ApiError } from './api';
import { listCandidates, listResults, proposeSyntheticCandidate, type MemoryScope, type SourceResult } from './memoryApi';
import { listRuns, type Run } from './workApi';

type Props = { projectId: string; reloadToken: number; onChanged: () => void };

function message(error: unknown): string {
  return error instanceof Error ? error.message : 'The request failed. Check saved state before retrying.';
}

export function SyntheticMemoryIntake({ projectId, reloadToken, onChanged }: Props) {
  const [runs, setRuns] = useState<Run[]>([]);
  const [results, setResults] = useState<SourceResult[]>([]);
  const [scope, setScope] = useState<MemoryScope>('project');
  const [selected, setSelected] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    void Promise.all([listRuns(projectId, '', controller.signal), listResults(projectId, controller.signal)])
      .then(([runRows, resultRows]) => {
        setRuns(runRows);
        setResults(resultRows);
        setLoading(false);
      }).catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === 'AbortError') return;
        setError(message(cause));
        setLoading(false);
      });
    return () => controller.abort();
  }, [projectId, reloadToken, reload]);

  const available = runs.filter((run) => run.status === 'done' &&
    (run.mode === 'synthetic_text' || run.mode === 'synthetic_code') &&
    !results.some((source) => source.run_id === run.id));

  async function propose() {
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const proposal = await proposeSyntheticCandidate(projectId, selected, scope);
      const [readResults, readCandidates] = await Promise.all([
        listResults(projectId), listCandidates(projectId),
      ]);
      if (!readResults.some((item) => item.id === proposal.result.id && item.run_id === selected) ||
          !readCandidates.some((item) => item.id === proposal.candidate.id &&
            item.source_result_id === proposal.result.id)) {
        throw new ApiError('Proposal returned, but readback did not confirm the source and candidate.', null);
      }
      setResults(readResults);
      setSelected('');
      setNotice('Synthetic result staged for operator review. Memory has not been published.');
      onChanged();
    } catch (cause) {
      setError(message(cause));
    } finally {
      setSaving(false);
    }
  }

  return <section className="memory-intake panel" aria-labelledby="synthetic-memory-heading">
    <div className="section-heading"><div><p className="eyebrow">Synthetic flow result</p>
      <h2 id="synthetic-memory-heading">Stage a completed run</h2>
      <p>Review a delivered Sales draft or verified Engineer to QA result before sharing it as memory.</p>
    </div></div>
    {notice && <p role="status" className="notice">{notice}</p>}
    {loading ? <p role="status">Loading synthetic runs…</p> : <>
      <label className="field"><span className="field-label">Completed synthetic run</span>
        <select value={selected} onChange={(event) => setSelected(event.target.value)}>
          <option value="">Choose a run</option>
          {available.map((run) => <option key={run.id} value={run.id}>
            {run.work_order_title} · {run.mode === 'synthetic_code' ? 'Engineer to QA' : 'Researcher to GTM to Sales'}
          </option>)}
        </select></label>
      <label className="field"><span className="field-label">Memory scope</span>
        <select value={scope} onChange={(event) => setScope(event.target.value as MemoryScope)}>
          <option value="project">This project only</option><option value="company">Company across projects</option>
        </select></label>
      <p className="quiet-note">Sales requires confirmed delivery. The candidate stays pending until you approve it.</p>
      {error && <p role="alert" className="form-error">{error}</p>}
      <button className="button button-primary" type="button" disabled={!selected || saving}
        onClick={() => { void propose(); }}>{saving ? 'Staging…' : 'Stage DreamCycle candidate'}</button>
      {available.length === 0 && <p className="quiet-note">No completed synthetic run is awaiting a result.</p>}
      <button className="button button-outline" type="button" disabled={saving}
        onClick={() => setReload((value) => value + 1)}>Refresh runs</button>
    </>}
  </section>;
}
