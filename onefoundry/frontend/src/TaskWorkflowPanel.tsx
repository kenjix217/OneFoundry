import { useEffect, useState } from 'react';
import { CapabilityReadiness } from './CapabilityReadiness';
import { activateTeam, cancelQueuedTeam, getTeamReadiness, type TeamReadiness } from './taskWorkflowApi';

type Props = { projectId: string; orderId: string; version: number; reloadKey: number; onActivated?: () => void;
  onStatusRead?: (status: string) => void; onStartWork?: () => void; startBusy?: boolean; startError?: string | null };

export function TaskWorkflowPanel({ projectId, orderId, version, reloadKey, onActivated, onStatusRead,
  onStartWork, startBusy = false, startError }: Props) {
  const [saved, setSaved] = useState<TeamReadiness | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    setSaved(null); setError(null);
    const refresh = () => {
      if (document.hidden) return;
      void getTeamReadiness(projectId, orderId, controller.signal).then((value) => {
        if (!controller.signal.aborted) { setSaved(value); setError(null); onStatusRead?.(value.status); }
      }).catch((cause: unknown) => {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : 'Readiness is unavailable.');
      });
    };
    refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [projectId, orderId, reloadKey, version, onStatusRead]);

  async function enable() {
    if (!saved?.ready_to_activate || busy) return;
    setBusy(true); setError(null);
    try { await activateTeam(projectId, orderId, version); onActivated?.(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'Team execution could not be enabled.'); }
    finally { setBusy(false); }
  }

  async function cancel() {
    if (busy) return;
    setBusy(true); setError(null);
    try { await cancelQueuedTeam(projectId, orderId, version); onActivated?.(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'Queued work could not be canceled.'); }
    finally { setBusy(false); }
  }

  return <section className="task-workflow-status" aria-label="Work execution status">
    <h3>Execution status</h3>
    {error && <p className="form-error" role="alert">Could not confirm workflow readiness: {error}</p>}
    {startError && <p className="form-error" role="alert">{startError}</p>}
    {!saved && !error && <p role="status">Checking saved execution state…</p>}
    {saved && <>
      {saved.ready_to_start && onStartWork && <>
        <button type="button" className="button button-primary" disabled={startBusy} onClick={onStartWork}>
          {startBusy ? 'Starting…' : 'Start work'}</button>
        <p>Start work saves your current message, admits this draft, and queues the team. Ordinary Send is for discussion.</p>
      </>}
      <p>Provider configuration is checked here. A live tool connection is proved only by a verified attempt and its receipts.</p>
      <p>{saved.running_attempts} active · {saved.queued_attempts} queued · {saved.confirmed_results} saved results · {saved.pending_approvals} pending approvals</p>
      {!!saved.capability_notes?.length && <details><summary>Execution capabilities</summary>
        <ul>{saved.capability_notes.map((note) => <li key={note}>{note}</li>)}</ul></details>}
      {saved.capability_readiness && <CapabilityReadiness report={saved.capability_readiness} />}
      {saved.prompt_context_bytes_limit !== undefined && <p>Prompt limit: {saved.prompt_context_bytes_limit === null
        ? 'Unlimited' : `${saved.prompt_context_bytes_limit.toLocaleString()} bytes`}</p>}
      {saved.execution_mode === 'team' && ['ready', 'running'].includes(saved.status) && saved.running_attempts === 0 && saved.queued_attempts === 0 && <p>No turn is running. {saved.next_step}</p>}
      {saved.execution_mode === 'legacy' && <p>Opening this task opens its saved chat. A chat reply or saved draft does not start specialist work.</p>}
      {saved.blockers.length > 0 && <details open={saved.status !== 'done'}><summary>{saved.status === 'done' ? 'Completed work and execution configuration' : saved.status === 'needs_review' ? 'Awaiting operator review' : saved.ready_to_start ? 'Saved draft admission' : 'Work cannot start yet'}</summary>
        <ul>{saved.blockers.map((item) => <li key={item}>{item}</li>)}</ul></details>}
      {saved.ready_to_activate && saved.execution_mode === 'legacy' && !onStartWork && <>
        <p>Enable team execution for this order, then send a message or continue to queue its first turn. Existing history stays attached.</p>
        <button type="button" className="button button-outline" disabled={busy} onClick={() => { void enable(); }}>
          {busy ? 'Enabling…' : 'Enable team execution'}</button>
      </>}
      {saved.status === 'needs_review' && <p>Agent turns finished. Review the saved deliverable and QA evidence before accepting the work order.</p>}
      {saved.execution_mode === 'team' && ['ready', 'running'].includes(saved.status) && saved.queued_attempts > 0 && saved.running_attempts === 0 &&
        <button type="button" className="button button-outline" disabled={busy} onClick={() => { void cancel(); }}>
          {busy ? 'Canceling…' : 'Cancel queued work'}</button>}
    </>}
  </section>;
}
