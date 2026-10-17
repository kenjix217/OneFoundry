import { useEffect, useState } from 'react';

import type { Profile } from './api';
import { getExternalCode, getExternalCodeArtifact, getExternalCodeProgress,
  type ExternalCodeAttempt, type ExternalCodeDetail } from './externalCodeApi';
import { describeActivity, workOrderStatusLabel } from './workOrderActivity';
import { parseSavedActionTrace, type ProviderProgress, type SavedActionTrace } from './workOrderTrace';
import { getLiveTextOutput, getTextOutputs, getWorkOrderActivity, listRuns,
  type LiveTextOutput, type Run, type RunEvent, type TextOutput, type WorkOrder } from './workApi';

type Props = { projectId: string; order: WorkOrder; profiles: Profile[];
  mode: 'conversation' | 'activity' | 'results' | 'details'; onOpenRun: (runId: string) => void };

function time(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function errorText(cause: unknown): string {
  return cause instanceof Error ? cause.message : 'Saved work could not be read.';
}

function runStatus(status: Run['status']): string {
  return status === 'running' ? 'Working' : status === 'done' ? 'Completed' :
    status === 'failed' ? 'Failed' : 'Outcome uncertain';
}

function attemptStatus(status: ExternalCodeAttempt['status']): string {
  return status === 'queued' ? 'Queued' : status === 'done' ? 'Completed' :
    status === 'failed' ? 'Failed' : 'Outcome uncertain';
}

function plainReason(reason: string): string {
  return reason.replaceAll('_', ' ').replaceAll('-', ' ');
}

function actionOutcome(outcome: SavedActionTrace['actions'][number]['outcome']): string {
  return outcome === 'timed_out' ? 'Timed out' : outcome === 'unknown' ? 'Outcome uncertain' :
    outcome === 'done' ? 'Completed' : 'Finished';
}

export function WorkOrderEvidenceView({ projectId, order, profiles, mode, onOpenRun }: Props) {
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [texts, setTexts] = useState<TextOutput[]>([]);
  const [liveTexts, setLiveTexts] = useState<LiveTextOutput[]>([]);
  const [coding, setCoding] = useState<ExternalCodeDetail[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    if (order.status !== 'running' || mode === 'details') return;
    const timer = window.setInterval(() => setReload((value) => value + 1), 10000);
    return () => window.clearInterval(timer);
  }, [order.status, mode]);

  useEffect(() => {
    if (mode === 'details') { setLoading(false); return; }
    const controller = new AbortController();
    setLoading(true); setError(null);
    void Promise.all([getWorkOrderActivity(projectId, order.id, controller.signal),
      listRuns(projectId, '', controller.signal)]).then(async ([activity, projectRuns]) => {
      const selected = projectRuns.filter((run) => run.work_order_id === order.id);
      const [scripted, live, code] = await Promise.all([
        mode === 'results' ? Promise.all(selected.filter((run) => run.mode === 'synthetic_text' && run.status === 'done')
          .map((run) => getTextOutputs(projectId, run.id, controller.signal))) : Promise.resolve([]),
        mode === 'results' ? Promise.all(selected.filter((run) => run.mode === 'live_text' && run.status === 'done')
          .map((run) => getLiveTextOutput(projectId, run.id, controller.signal))) : Promise.resolve([]),
        Promise.all(selected.filter((run) => run.mode === 'external_code')
          .map((run) => getExternalCode(projectId, run.id, controller.signal))),
      ]);
      if (controller.signal.aborted) return;
      setEvents(activity); setRuns(selected); setTexts(scripted.flat());
      setLiveTexts(live.filter((item): item is LiveTextOutput => item !== null));
      setCoding(code); setLoading(false);
    }).catch((cause: unknown) => {
      if (controller.signal.aborted) return;
      setError(errorText(cause)); setLoading(false);
    });
    return () => controller.abort();
  }, [projectId, order.id, mode, reload]);

  const activity = events.map(describeActivity);
  const agentName = (id: string): string => profiles.find((item) => item.id === id)?.name ?? 'Agent';

  if (mode === 'details') return <section className="task-evidence" aria-label="Work order details">
    <h3>Assignment</h3><p>{order.instructions}</p>
    <p>Assigned to <strong>{agentName(order.profile_id)}</strong> · {workOrderStatusLabel(order.status)}</p>
    <p>Expected deliverable: {order.deliverable_kind.replaceAll('_', ' ')}</p>
    {order.origin_signal_id && <p className="notice" role="note">Created automatically by an Automation rule
      (step {order.chain_depth} of its event chain). Open Automation to see the event that triggered it.</p>}
    {order.acceptance_checks.length > 0 && <><h4>Acceptance checks</h4><ul>{order.acceptance_checks.map((check, index) =>
      <li key={`${index}-${check}`}>{check}</li>)}</ul></>}
    <p>Assignment version {order.version}</p>
  </section>;

  return <section className="task-evidence" aria-label={mode === 'conversation' ? 'Work underway' : mode === 'activity' ? 'Task activity' : 'Task results'}>
    <div className="task-evidence-heading"><h3>{mode === 'conversation' ? 'Work underway' : mode === 'activity' ? 'Activity' : 'Results'}</h3>
      <button className="button button-quiet" type="button" onClick={() => setReload((value) => value + 1)}>Refresh</button></div>
    {loading && <p role="status">Loading saved work…</p>}
    {error && <p role="alert" className="form-error">{error}</p>}
    {!loading && !error && mode !== 'results' && activity.length === 0 && coding.length === 0 &&
      <p>No work activity has been recorded yet.</p>}
    {!loading && !error && mode !== 'results' && <ol className="task-activity-list">
      {(mode === 'conversation' ? activity.slice(-6) : activity).map((item) => <li key={item.id}>
        <div className="task-activity-head"><strong>{item.title}</strong><time dateTime={item.at}>{time(item.at)}</time></div>
        <small>System activity{item.runId ? ` · ${agentName(runs.find((run) => run.id === item.runId)?.profile_id ?? '')}` : ''}</small>
        <p>{item.detail}</p>
        {item.runId && <button className="button button-quiet" type="button" onClick={() => onOpenRun(item.runId as string)}>Open run</button>}

      </li>)}
    </ol>}
    {!loading && !error && mode !== 'results' && coding.flatMap((run) => run.attempts).map((attempt) =>
      <CodingActions key={attempt.id} projectId={projectId} attempt={attempt} showProgress={mode === 'conversation'} />)}
    {!loading && !error && mode === 'results' && <>
      {runs.length === 0 && <p>No run has been recorded for this work order.</p>}
      {runs.map((run) => <article className="task-result" key={run.id}>
        <div className="task-activity-head"><strong>{agentName(run.profile_id)} · {run.mode === 'external_code' ? 'Coding' :
          run.mode === 'manual_planning' ? 'Manual record' : 'Text result'}</strong><span>{runStatus(run.status)}</span></div>
        {run.status === 'unknown' && <p>Outcome uncertain. Check the saved attempt before starting another one.</p>}
        {run.status === 'failed' && <p>The attempt failed: {run.outcome_reason ? plainReason(run.outcome_reason) : 'No reason saved.'}</p>}
        {texts.filter((output) => output.run_id === run.id).map((output) => <p key={output.id}>{output.text}</p>)}
        {liveTexts.filter((output) => output.run_id === run.id).map((output) => <div key={output.id}>
          <p>{output.text}</p>
          {output.automation_decision && !['none', 'clarify'].includes(output.automation_decision.status) &&
            <p className="quiet-note">Automation decision: {output.automation_decision.status}.</p>}
        </div>)}
        {coding.filter((item) => item.run.id === run.id).flatMap((item) => item.attempts).map((attempt) =>
          <div key={attempt.id}><p>Runner {attemptStatus(attempt.status)} · {attempt.tests.length} recorded checks · {attempt.artifacts.length} saved files</p>
            <CodingActions projectId={projectId} attempt={attempt} /></div>)}
        <button className="button button-outline" type="button" onClick={() => onOpenRun(run.id)}>View full run</button>
      </article>)}
    </>}
  </section>;
}

export function CodingActions({ projectId, attempt, showProgress = false }: {
  projectId: string; attempt: ExternalCodeAttempt; showProgress?: boolean;
}) {
  const [savedFiles, setSavedFiles] = useState<Record<string, string>>({});
  const [trace, setTrace] = useState<SavedActionTrace | null>(null);
  const [liveProgress, setLiveProgress] = useState<Array<ProviderProgress & { sequence: number }>>([]);
  const [progressError, setProgressError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [evidenceLoaded, setEvidenceLoaded] = useState(false);
  const available = attempt.tests.length > 0 || attempt.artifacts.length > 0;
  const traceArtifact = attempt.artifacts.find((item) => item.kind === 'trace');
  const traceArtifactId = traceArtifact?.id;
  const traceArtifactSha = traceArtifact?.sha256;
  const traceArtifactBytes = traceArtifact?.byte_size;
  async function openEvidence() {
    const artifacts = attempt.artifacts.filter((item) => ['log', 'diff', 'trace'].includes(item.kind));
    if (!artifacts.length || evidenceLoaded) return;
    try {
      const previews = await Promise.all(artifacts.map((item) => getExternalCodeArtifact(projectId, attempt.run_id, item.id)));
      const next: Record<string, string> = {};
      let nextTrace: SavedActionTrace | null = null;
      previews.forEach((saved, index) => {
        const artifact = artifacts[index];
        if (saved.sha256 !== artifact.sha256 || saved.byte_size !== artifact.byte_size || saved.kind !== artifact.kind) {
          throw new Error('Saved action evidence changed during readback.');
        }
        if (artifact.kind === 'trace') nextTrace = parseSavedActionTrace(saved.text);
        else next[artifact.kind] = saved.text;
      });
      setTrace(nextTrace);
      setSavedFiles(next);
      setEvidenceLoaded(true);
    } catch (cause) { setError(errorText(cause)); }
  }
  useEffect(() => {
    if (!showProgress) return;
    if (!traceArtifactId) return;
    void getExternalCodeArtifact(projectId, attempt.run_id, traceArtifactId).then((saved) => {
      if (saved.sha256 !== traceArtifactSha || saved.byte_size !== traceArtifactBytes || saved.kind !== 'trace') {
        throw new Error('Saved action evidence changed during readback.');
      }
      setTrace(parseSavedActionTrace(saved.text));
    }).catch((cause: unknown) => setError(errorText(cause)));
  }, [showProgress, projectId, attempt.run_id, traceArtifactId, traceArtifactSha, traceArtifactBytes]);
  useEffect(() => {
    if (!showProgress) return;
    const controller = new AbortController();
    let nextSequence = 0;
    let inFlight = false;
    let terminal = false;
    async function poll() {
      if (inFlight || terminal || controller.signal.aborted) return;
      inFlight = true;
      try {
        const saved = await getExternalCodeProgress(projectId, attempt.run_id, attempt.id, nextSequence, controller.signal);
        if (controller.signal.aborted) return;
        nextSequence = saved.latest_sequence;
        terminal = saved.terminal;
        setProgressError(null);
        if (saved.items.length) setLiveProgress((current) => {
          const merged = new Map(current.map((item) => [item.sequence, item]));
          saved.items.forEach((item) => merged.set(item.sequence, item));
          return [...merged.values()].sort((left, right) => left.sequence - right.sequence).slice(0, 24);
        });
      } catch (cause) {
        if (!controller.signal.aborted) setProgressError(errorText(cause));
      } finally { inFlight = false; }
    }
    void poll();
    const timer = attempt.status === 'queued' ? window.setInterval(() => { void poll(); }, 3000) : null;
    return () => { controller.abort(); if (timer !== null) window.clearInterval(timer); };
  }, [showProgress, projectId, attempt.run_id, attempt.id, attempt.status]);
  const visibleProgress = liveProgress.length ? liveProgress : trace?.progress ?? [];
  return <div className="task-action-trace">
    {showProgress && <section aria-label="Work updates">
      <h4>Work updates</h4>
      {progressError && <p role="alert">Live updates are unavailable: {progressError}</p>}
      {visibleProgress.length ? <ol className="task-activity-list">{visibleProgress.map((item, index) => <li key={index}>
        <strong>{item.kind === 'runner_update' ? 'Runner update' : item.kind === 'agent_update' ? 'Agent update' : 'Tool activity'}</strong>
        <p>{item.text}</p>
        {item.kind === 'tool_activity' && <details><summary>{item.tool === 'shell' || item.tool === 'Bash' ? 'Shell command' : 'Tool details'}</summary>
          <p>{item.command ?? `${item.tool} arguments and output were withheld from this public trace.`}</p>
        </details>}
      </li>)}</ol> : <p>{attempt.status === 'queued'
        ? 'Waiting for coding agent updates…' : 'No safe intermediate CLI updates were available for this attempt.'}</p>}
      <small>This is a partial summary of runner stages and CLI output. Private reasoning and tool payloads are excluded.
        {trace?.omitted_progress ? ` ${trace.omitted_progress} updates were omitted.` : ''}</small>
    </section>}
    <details onToggle={(event) => { if (event.currentTarget.open) void openEvidence(); }}>
    <summary>Actions in this run · {available ? 'Partial record' : 'Unavailable'}</summary>
    {error && <p role="alert">{error}</p>}
    {trace ? <><p>Runner observed · {trace.actions.length} saved actions. {trace.progress.length
      ? `${trace.progress.length} safe runner and CLI updates were summarized; the complete provider tool record is unavailable`
      : 'Provider tool calls and intermediate agent updates were not captured'}
      {trace.omitted_actions ? `; ${trace.omitted_actions} runner actions were omitted by the size limit` : ''}.</p>
      <ol>{trace.actions.map((action, index) => <li key={index}>
        <strong>{action.kind === 'provider_invocation' ? 'Coding agent launched' : 'Shell command'}</strong>
        {' · '}{actionOutcome(action.outcome)}{action.exit_code === null ? '' : ` · exit ${action.exit_code}`}
        <details><summary>{action.command.join(' ')} · {action.cwd}</summary>
          <p>Started {time(action.started_at)} · finished {time(action.finished_at)}</p>
          {action.stdout && <><strong>Output</strong><pre>{action.stdout}</pre></>}
          {action.stderr && <><strong>Error output</strong><pre>{action.stderr}</pre></>}
          {action.output_truncated && <p>Captured output was truncated.</p>}
        </details>
      </li>)}</ol></> : <p>{attempt.artifacts.some((item) => item.kind === 'trace') && !error
      ? 'Loading ordered runner actions…'
      : 'An ordered command trace was not saved for this attempt. The checks and saved files below are partial evidence.'}</p>}
    {attempt.tests.length > 0 && <details><summary>Test receipts · {attempt.tests.length}</summary><ol>
      {attempt.tests.map((check, index) => <li key={`${check.name}-${index}`}>
      <strong>{check.name.replaceAll('_', ' ')}</strong> · {check.status} · exit {check.exit_code}
      {!trace && attempt.fixture_id === 'engineer_qa_v1' && <details><summary>Test command</summary>
        <code>python -m unittest discover -s tests -v</code><p>Executed in the disposable fixture repository.</p></details>}
    </li>)}</ol></details>}
    {attempt.artifacts.filter((item) => item.kind === 'log' || item.kind === 'diff').map((item) =>
      <details key={item.id}><summary>{item.kind === 'log' ? 'Saved runner log' : 'Saved code patch'}</summary>
        {savedFiles[item.kind] ? <pre>{savedFiles[item.kind]}</pre> : <p>{error ? 'Could not read this file.' : 'Loading saved file…'}</p>}
      </details>)}
    {!available && <p>No individual actions were saved for this attempt. Review its status and reason in Results.</p>}
    </details>
  </div>;
}
