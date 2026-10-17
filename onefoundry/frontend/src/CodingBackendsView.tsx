import { useEffect, useState } from 'react';

import { ApiError, type Profile } from './api';
import { getExternalCode, getExternalCodeEligibility, type CodingAdapterId, type ExternalCodeEligibility } from './externalCodeApi';
import { listProjectSources, type ProjectSource } from './sourceApi';
import { listRuns, listWorkOrders, type Run, type WorkOrder } from './workApi';

type Destination = 'work' | 'integrations' | 'activity';
type Props = { projectId: string; profiles: Profile[]; onNavigate: (view: Destination, runId?: string) => void };
type Snapshot = { orders: WorkOrder[]; runs: Run[]; sources: ProjectSource[] };
const ADAPTERS: { id: CodingAdapterId; label: string }[] = [
  { id: 'codex_cli', label: 'Codex CLI' }, { id: 'claude_code', label: 'Claude Code' },
];

function message(error: unknown): string {
  return error instanceof ApiError ? error.message : 'Could not read saved coding state. Check the local service and refresh.';
}

export function CodingBackendsView({ projectId, profiles, onNavigate }: Props) {
  const [saved, setSaved] = useState<Snapshot | null>(null);
  const [selectedOrderId, setSelectedOrderId] = useState('');
  const [eligibility, setEligibility] = useState<ExternalCodeEligibility | null>(null);
  const [loadingEligibility, setLoadingEligibility] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [eligibilityError, setEligibilityError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [runKinds, setRunKinds] = useState<Record<string, 'offline' | 'coding' | 'synthetic'>>({});

  useEffect(() => {
    const controller = new AbortController();
    setSaved(null); setError(null); setEligibility(null); setSelectedOrderId('');
    void Promise.all([
      listWorkOrders(projectId, controller.signal), listRuns(projectId, '', controller.signal),
      listProjectSources(projectId, controller.signal),
    ]).then(([orders, runs, sources]) => {
      if (controller.signal.aborted) return;
      setSaved({ orders, runs, sources });
      const engineerIds = new Set(profiles.filter((profile) => profile.kind === 'builtin' && profile.name === 'Engineer').map((profile) => profile.id));
      const ready = orders.filter((order) => order.status === 'ready' && engineerIds.has(order.profile_id));
      setSelectedOrderId((current) => ready.some((order) => order.id === current) ? current : ready[0]?.id ?? '');
    }).catch((failure: unknown) => {
      if (!(failure instanceof DOMException && failure.name === 'AbortError')) setError(message(failure));
    });
    return () => controller.abort();
  }, [projectId, profiles, refresh]);

  useEffect(() => {
    setEligibility(null); setEligibilityError(null);
    if (!selectedOrderId) { setLoadingEligibility(false); return; }
    const controller = new AbortController();
    setLoadingEligibility(true);
    void getExternalCodeEligibility(projectId, selectedOrderId, controller.signal).then((result) => {
      if (controller.signal.aborted) return;
      setEligibility(result); setLoadingEligibility(false);
    }).catch((failure: unknown) => {
      if (failure instanceof DOMException && failure.name === 'AbortError') return;
      setEligibilityError(message(failure)); setLoadingEligibility(false);
    });
    return () => controller.abort();
  }, [projectId, selectedOrderId, refresh]);

  const engineerIds = new Set(profiles.filter((profile) => profile.kind === 'builtin' && profile.name === 'Engineer').map((profile) => profile.id));
  const readyOrders = saved?.orders.filter((order) => order.status === 'ready' && engineerIds.has(order.profile_id)) ?? [];
  const codingRuns = saved?.runs.filter((run) => run.mode === 'external_code') ?? [];

  useEffect(() => {
    const controller = new AbortController();
    setRunKinds({});
    void Promise.all((saved?.runs.filter((run) => run.mode === 'external_code') ?? []).map(async (run) => {
      try {
        const detail = await getExternalCode(projectId, run.id, controller.signal);
        return [run.id, detail.attempts.some((attempt) => attempt.target_id === 'github_snapshot_codex_fixture_v1') ? 'synthetic' :
          detail.attempts.some((attempt) => attempt.adapter_id === 'offline_probe') ? 'offline' : 'coding'] as const;
      } catch { return null; }
    })).then((items) => {
      if (!controller.signal.aborted) setRunKinds(Object.fromEntries(items.filter((item) => item !== null)));
    });
    return () => controller.abort();
  }, [projectId, saved]);

  return <section className="coding-backends-page" aria-labelledby="coding-backends-title">
    <div className="view-heading"><div><p className="eyebrow">Execution Gateway</p><h1 id="coding-backends-title">Coding agents</h1>
      <p>Inspect saved repository context, work-order eligibility, and coding receipts for this project.</p></div>
      <button className="button button-outline" type="button" onClick={() => setRefresh((value) => value + 1)}>Refresh saved state</button></div>
    {!saved && !error && <p className="setup-readback" role="status">Reading coding state…</p>}
    {error && <div className="setup-readback" role="alert"><p>{error}</p><button className="button button-outline" type="button" onClick={() => setRefresh((value) => value + 1)}>Try again</button></div>}
    {saved && <>
      <section className="coding-backends-panel panel" aria-labelledby="coding-source-title">
        <div className="coding-backends-head"><div><p className="eyebrow">01 / Project context</p><h2 id="coding-source-title">Selected repositories</h2></div>
          <button className="button button-outline" type="button" onClick={() => onNavigate('integrations')}>Open repositories</button></div>
        {saved.sources.length === 0 ? <p>No repository selected for this project.</p> : <ul className="coding-source-list">{saved.sources.map((source) => <li key={source.id}>
          <strong>{source.display_name}</strong><span>{source.kind === 'local' ? 'Local' : 'GitHub'} · {source.branch}</span>
          <small>{source.availability === 'unavailable' ? 'Unavailable at last readback' : 'Verified at selection; current revision has not been rechecked'}</small>
        </li>)}</ul>}
        <p className="quiet-note">Live coding CLI attempts use a disposable fixture repository. Offline source probes bind one saved local source and its exact revision.</p>
      </section>

      <section className="coding-backends-panel panel" aria-labelledby="coding-readiness-title">
        <div className="coding-backends-head"><div><p className="eyebrow">02 / Work-order eligibility</p><h2 id="coding-readiness-title">Backend readiness</h2></div>
          <button className="button button-outline" type="button" onClick={() => onNavigate('work')}>Open Work orders</button></div>
        <p>Availability below is the current eligibility result for one ready Engineer work order. The trusted local worker uses the operator's CLI login and host access.</p>
        {readyOrders.length === 0 ? <p className="setup-attention">No ready Engineer work order is available to check. Prepare one in Work orders.</p> :
          <label className="field coding-order-select"><span className="field-label">Ready Engineer work order</span><select value={selectedOrderId} onChange={(event) => setSelectedOrderId(event.target.value)}>
            {readyOrders.map((order) => <option key={order.id} value={order.id}>{order.title}</option>)}</select></label>}
        {loadingEligibility && <p role="status">Checking backend eligibility…</p>}
        {eligibilityError && <p className="form-error" role="alert">{eligibilityError}</p>}
        <div className="coding-backend-grid">{ADAPTERS.map((adapter) => {
          const result = eligibility?.adapters.find((item) => item.adapter_id === adapter.id);
          return <article key={adapter.id} className="coding-backend-card"><h3>{adapter.label}</h3>
            <strong>{result ? result.available ? 'Eligible for this work order' : 'Unavailable' : 'Not checked'}</strong>
            <p>{result ? result.reason ?? 'Runner reported this adapter available for the selected work order.' : selectedOrderId ? 'Waiting for eligibility readback.' : 'Select a ready Engineer work order to check.'}</p>
          </article>;
        })}</div>
        {eligibility?.reason && <p className="setup-attention">Overall: {eligibility.reason}</p>}
        <p className="quiet-note">A model account or saved repository does not make a CLI ready. Start the bounded coding run from Work orders, then review its saved activity and result before applying any change elsewhere.</p>
        <details className="setup-readback"><summary>Local coding test</summary>
          <p>A separate ready built-in Engineer work order can bind an approved, completed fake GitHub v3 snapshot. Work orders reads the exact eligible snapshots and records an idempotent launch. A pinned Codex CLI executable uses a local fake TLS provider; this does not use a live provider account, fetch real GitHub data, or control a desktop session.</p>
          <button className="button button-outline" type="button" onClick={() => onNavigate('work')}>Select snapshot in Work orders</button>
        </details>
      </section>

      <section className="coding-backends-panel panel" aria-labelledby="coding-receipts-title">
        <div className="coding-backends-head"><div><p className="eyebrow">03 / Recorded attempts</p><h2 id="coding-receipts-title">Coding receipts</h2></div>
          <button className="button button-outline" type="button" onClick={() => onNavigate('activity')}>Open Activity</button></div>
        {codingRuns.length === 0 ? <p>No coding or offline source probe runs have been recorded for this project.</p> :
          <ul className="coding-run-list">{codingRuns.map((run) => <li key={run.id}><div><strong>{run.work_order_title ?? 'Work order'}</strong>
            <span>{runKinds[run.id] === 'synthetic' ? 'Managed synthetic Codex CLI · local fake TLS provider' :
              runKinds[run.id] === 'offline' ? 'Offline local source probe · no coding provider' :
              runKinds[run.id] === 'coding' ? 'External coding agent' : 'Attempt type loading or unavailable'}</span>
            <span>{run.status === 'unknown' ? 'Unknown outcome; reconcile in Activity' : run.status === 'running' ? 'Running or awaiting a runner receipt' : `${run.status} · inspect saved receipt`}</span></div>
            <button className="button button-outline" type="button" onClick={() => onNavigate('activity', run.id)}>Open run in Activity</button></li>)}</ul>}
      </section>
    </>}
  </section>;
}
