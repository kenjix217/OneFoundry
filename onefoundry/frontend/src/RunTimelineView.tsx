import { describeActivity } from './workOrderActivity';
import { WorkOrderTrigger } from './WorkOrderTrigger';
import { useEffect, useMemo, useState, type FormEvent } from 'react';

import { ApiError, type Profile } from './api';
import { BrowserStaticPanel } from './BrowserStaticPanel';
import { CodeRunPanel } from './CodeRunPanel';
import type { CodeDetail } from './codeApi';
import { ExternalCodePanel } from './ExternalCodePanel';
import { InferenceLogPanel } from './InferenceLogPanel';
import type { ExternalCodeDetail } from './externalCodeApi';
import { snapshotCodeStatus } from './snapshotCodeStatus';
import { listResults, type SourceResult } from './memoryApi';
import { SalesPanel } from './SalesPanel';
import { WorkspaceArtifactPanel } from './WorkspaceArtifactPanel';
import { draftQaReviewWorkOrder, finishRun, getLiveTextOutput, getQaReviewWorkOrder, getRun, getTextOutputs, handoffRun, listRuns, listWorkOrders,
  type LiveTextOutput, type Run, type RunDetail, type RunStatus, type TextOutput, type WorkOrder } from './workApi';

type Props = { projectId: string; profiles: Profile[]; initialRunId: string | null;
  initialOrderId?: string | null; onOpenTask?: (orderId: string) => void };

function readable(value: string): string {
  return value.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function time(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Could not complete the request. Check the saved state before retrying.';
}

function tokens(value: number | null): string {
  return value === null ? 'Usage unavailable' : `${value} tokens`;
}

function tokenDetail(label: string, value: number | null): string {
  return value === null ? `${label} usage unavailable` : `${label} ${value} tokens`;
}

function liveSkillSource(run: RunDetail, snapshot: RunDetail['skill_snapshots'][number]): string {
  const started = run.events.find((event) => event.event_type === 'live_text_started' && event.run_id === run.id);
  const skills = started?.payload.skills;
  if (!Array.isArray(skills)) return 'Saved selection';
  const matched = skills.find((item: unknown) => {
    if (typeof item !== 'object' || item === null || Array.isArray(item)) return false;
    const entry = item as Record<string, unknown>;
    return entry.skill_id === snapshot.skill_id && entry.version === snapshot.version && entry.sha256 === snapshot.sha256;
  }) as Record<string, unknown> | undefined;
  return matched?.selection_source === 'automatic' ? 'Automatic' : matched?.selection_source === 'pinned' ? 'Pinned' : 'Saved selection';
}

function liveCost(output: LiveTextOutput | null): string {
  if (output?.provider !== 'grok' || output.cost_usd_ticks === null) return 'Cost not available';
  const ticks = BigInt(output.cost_usd_ticks);
  const whole = ticks / 10000000000n;
  const decimal = String(ticks % 10000000000n).padStart(10, '0').replace(/0+$/, '');
  return `Cost $${whole}${decimal ? `.${decimal}` : ''}`;
}

function modeName(mode: Run['mode']): string {
  switch (mode) {
    case 'manual_planning': return 'Manual planning';
    case 'synthetic_text': return 'Synthetic text';
    case 'synthetic_code': return 'Synthetic Engineer → QA';
    case 'live_text': return 'Live provider text';
    case 'external_code': return 'External execution attempt';
  }
}

function savedResult(run: RunDetail, textOutputs: TextOutput[], liveOutput: LiveTextOutput | null,
  codeDetail: CodeDetail | null, externalDetail: ExternalCodeDetail | null, operatorResults: SourceResult[] | null): string {
  if (run.mode === 'manual_planning') {
    if (run.status === 'done' && operatorResults === null) return 'Operator result readback unavailable';
    if (operatorResults?.length) return `${operatorResults.length} saved operator ${operatorResults.length === 1 ? 'result' : 'results'} · no agent or model result`;
    return 'Planning record only · no agent or model result';
  }
  if (run.mode === 'synthetic_text') return `${textOutputs.length} saved scripted text ${textOutputs.length === 1 ? 'output' : 'outputs'}`;
  if (run.mode === 'live_text') return liveOutput ? 'Saved provider text output' : 'No confirmed saved provider text output';
  if (run.mode === 'external_code') {
    if (!externalDetail || externalDetail.run.id !== run.id) return 'Coding attempt evidence loading or unavailable';
    const artifacts = externalDetail.attempts.flatMap((attempt) => attempt.artifacts);
    const diffs = artifacts.filter((artifact) => artifact.kind === 'diff').length;
    const tests = externalDetail.attempts.flatMap((attempt) => attempt.tests);
    const testSummary = tests.length ? `${tests.filter((test) => test.status === 'passed').length} passed, ${tests.filter((test) => test.status === 'failed').length} failed tests` : 'Tests not reported';
    const kind = externalDetail.attempts.some((attempt) => attempt.target_id === 'github_snapshot_codex_fixture_v1') ? 'managed synthetic Codex CLI' :
      externalDetail.attempts.some((attempt) => attempt.adapter_id === 'offline_probe') ? 'offline source probe' : 'coding';
    if (kind === 'managed synthetic Codex CLI' && run.status === 'done') {
      const before = tests.find((test) => test.name === 'file_text_before');
      const after = tests.find((test) => test.name === 'file_text_after');
      return `Saved synthetic receipt · baseline check ${before?.status ?? 'unavailable'} · candidate check ${after?.status ?? 'unavailable'} · ${diffs} saved ${diffs === 1 ? 'diff' : 'diffs'}`;
    }
    return `${externalDetail.attempts.length} saved ${kind} ${externalDetail.attempts.length === 1 ? 'attempt' : 'attempts'} · ${testSummary} · ${diffs} saved ${diffs === 1 ? 'diff' : 'diffs'} · ${artifacts.length} saved ${artifacts.length === 1 ? 'artifact' : 'artifacts'}`;
  }
  if (!codeDetail || codeDetail.run.id !== run.id) return 'Runner evidence loading or unavailable';
  const checkSummary = (stage: 'engineer' | 'qa' | 'merge', label: string): string => {
    const tests = codeDetail.jobs.filter((job) => job.stage === stage).flatMap((job) => job.tests);
    return tests.length
      ? `${label} checks: ${tests.filter((test) => test.status === 'passed').length} passed, ${tests.filter((test) => test.status === 'failed').length} failed`
      : `${label} checks not recorded`;
  };
  const artifacts = codeDetail.jobs.flatMap((job) => job.artifacts);
  const diffs = artifacts.filter((artifact) => artifact.kind === 'diff').length;
  const checks = [checkSummary('qa', 'QA'), checkSummary('engineer', 'Engineer')];
  if (codeDetail.jobs.some((job) => job.stage === 'merge')) checks.push(checkSummary('merge', 'Merge'));
  return `${codeDetail.jobs.length} saved runner ${codeDetail.jobs.length === 1 ? 'job' : 'jobs'} · ${checks.join(' · ')} · ${diffs} saved ${diffs === 1 ? 'diff' : 'diffs'} · ${artifacts.length} saved ${artifacts.length === 1 ? 'artifact' : 'artifacts'}`;
}

export function RunTimelineView({ projectId, profiles, initialRunId, initialOrderId = null, onOpenTask }: Props) {
  const [runs, setRuns] = useState<Run[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(initialRunId);
  const [scopeOrderId, setScopeOrderId] = useState<string | null>(initialOrderId);
  const [mobileDetailOpen, setMobileDetailOpen] = useState(false);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [currentOrder, setCurrentOrder] = useState<WorkOrder | null>(null);
  const [ownerReadError, setOwnerReadError] = useState<string | null>(null);
  const [textOutputs, setTextOutputs] = useState<TextOutput[]>([]);
  const [liveOutput, setLiveOutput] = useState<LiveTextOutput | null>(null);
  const [operatorResults, setOperatorResults] = useState<SourceResult[] | null>(null);
  const [codeDetail, setCodeDetail] = useState<CodeDetail | null>(null);
  const [externalDetail, setExternalDetail] = useState<ExternalCodeDetail | null>(null);
  const [query, setQuery] = useState('');
  const [search, setSearch] = useState('');
  const [reload, setReload] = useState(0);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [handoffTo, setHandoffTo] = useState('');
  const [outcome, setOutcome] = useState<Exclude<RunStatus, 'running'>>('done');
  const [reason, setReason] = useState('');
  const [saving, setSaving] = useState(false);
  const [qaDraftFields, setQaDraftFields] = useState<Record<string, { title: string; instructions: string }>>({});
  const [qaDrafts, setQaDrafts] = useState<Record<string, WorkOrder>>({});

  useEffect(() => { setSelectedId(initialRunId); }, [initialRunId]);
  useEffect(() => { setScopeOrderId(initialOrderId); setMobileDetailOpen(!!initialRunId); }, [initialOrderId, initialRunId]);

  const visibleRuns = useMemo(() => scopeOrderId ? runs.filter((run) => run.work_order_id === scopeOrderId) : runs,
    [runs, scopeOrderId]);

  useEffect(() => {
    if (loading) return;
    setSelectedId((current) => current && visibleRuns.some((run) => run.id === current)
      ? current : visibleRuns[0]?.id ?? null);
  }, [loading, visibleRuns]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    void listRuns(projectId, search, controller.signal).then((items) => {
      setRuns(items);
      setLoading(false);
    }).catch((cause: unknown) => {
      if (cause instanceof DOMException && cause.name === 'AbortError') return;
      setError(errorMessage(cause));
      setLoading(false);
    });
    return () => controller.abort();
  }, [projectId, search, reload]);

  useEffect(() => {
    if (!selectedId) { setDetail(null); setCurrentOrder(null); setOwnerReadError(null); setTextOutputs([]); setLiveOutput(null); setOperatorResults(null); setExternalDetail(null); return; }
    const controller = new AbortController();
    setDetailLoading(true);
    setDetail(null);
    setCurrentOrder(null);
    setOwnerReadError(null);
    setTextOutputs([]);
    setLiveOutput(null);
    setOperatorResults(null);
    setExternalDetail(null);
    setActionError(null);
    setQaDrafts({});
    setQaDraftFields({});
    void getRun(projectId, selectedId, controller.signal).then(async (item) => {
      setDetail(item);
      const qaRequests = item.events.filter((step) => step.event_type === 'qa_review_requested' && step.run_id === item.id);
      const savedQaDrafts = await Promise.all(qaRequests.map(async (step) =>
        ({ eventId: step.id, order: await getQaReviewWorkOrder(projectId, item.id, step.id, controller.signal) })));
      const outputs = item.mode === 'synthetic_text' ? await getTextOutputs(projectId, item.id, controller.signal) : [];
      const providerOutput = item.mode === 'live_text' && item.status === 'done'
        ? await getLiveTextOutput(projectId, item.id, controller.signal) : null;
      let savedOperatorResults: SourceResult[] | null = [];
      if (item.mode === 'manual_planning' && item.status === 'done') {
        try {
          savedOperatorResults = (await listResults(projectId, controller.signal))
            .filter((result) => result.run_id === item.id && result.source_kind === 'operator_entered');
        } catch {
          if (controller.signal.aborted) return;
          savedOperatorResults = null;
        }
      }
      let owner: WorkOrder | null = null;
      let ownerError: string | null = null;
      if (item.mode === 'manual_planning' && item.status === 'running') {
        try {
          owner = (await listWorkOrders(projectId, controller.signal)).find((order) => order.id === item.work_order_id) ?? null;
          if (!owner) ownerError = 'The current work order was not found.';
        } catch (cause) {
          if (controller.signal.aborted) return;
          ownerError = errorMessage(cause);
        }
      }
      if (controller.signal.aborted) return;
      setQaDrafts(Object.fromEntries(savedQaDrafts.filter((entry) => entry.order !== null)
        .map((entry) => [entry.eventId, entry.order as WorkOrder])));
      setCurrentOrder(owner);
      setOwnerReadError(ownerError);
      setTextOutputs(outputs);
      setLiveOutput(providerOutput);
      setOperatorResults(savedOperatorResults);
      setDetailLoading(false);
    }).catch((cause: unknown) => {
      if (cause instanceof DOMException && cause.name === 'AbortError') return;
      setDetail(null);
      setActionError(errorMessage(cause));
      setDetailLoading(false);
    });
    return () => controller.abort();
  }, [projectId, selectedId, reload]);

  async function readback(runId: string, expected: (run: RunDetail) => boolean) {
    const [item, items] = await Promise.all([getRun(projectId, runId), listRuns(projectId, search)]);
    if (!expected(item) || !items.some((run) => run.id === runId)) {
      throw new ApiError('The action returned, but run readback did not confirm it. Check the saved state before retrying.', null);
    }
    setDetail(item);
    setRuns(items);
  }

  async function checkManualOrder(workOrderId: string): Promise<void> {
    const order = (await listWorkOrders(projectId)).find((item) => item.id === workOrderId) ?? null;
    setCurrentOrder(order);
    setOwnerReadError(order ? null : 'The current work order was not found.');
    if (!order || order.status !== 'running') {
      throw new ApiError(order?.status === 'blocked'
        ? `This work order is blocked: ${order.block_reason ?? 'No reason recorded'}. Resume it from Work orders before continuing.`
        : 'The work order is no longer running. Review its saved state before continuing.', null);
    }
  }

  async function refreshOwnerAfterActionError(workOrderId: string, cause: unknown): Promise<void> {
    setCurrentOrder(null);
    try {
      const order = (await listWorkOrders(projectId)).find((item) => item.id === workOrderId) ?? null;
      setCurrentOrder(order);
      setOwnerReadError(order ? null : 'The current work order was not found.');
      setActionError(`${errorMessage(cause)} Work order state was refreshed; review it before retrying.`);
    } catch (readbackError) {
      setOwnerReadError(errorMessage(readbackError));
      setActionError(`${errorMessage(cause)} Work order readback also failed: ${errorMessage(readbackError)}`);
    }
  }

  async function submitHandoff(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!detail || !handoffTo || !currentOrder || currentOrder.status !== 'running') return;
    setSaving(true);
    setActionError(null);
    setNotice(null);
    try {
      await checkManualOrder(detail.work_order_id);
      await handoffRun(projectId, detail.id, handoffTo);
      await readback(detail.id, (item) => item.profile_id === handoffTo && item.events.length > detail.events.length);
      setHandoffTo('');
      setNotice('Profile handoff recorded. No agent was called.');
    } catch (cause) {
      await refreshOwnerAfterActionError(detail.work_order_id, cause);
    } finally {
      setSaving(false);
    }
  }

  async function submitFinish(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!detail || !currentOrder || currentOrder.status !== 'running') return;
    setSaving(true);
    setActionError(null);
    setNotice(null);
    try {
      await checkManualOrder(detail.work_order_id);
      await finishRun(projectId, detail.id, outcome, reason.trim());
      await readback(detail.id, (item) => item.status === outcome);
      setReason('');
      setNotice('Manual planning outcome recorded.');
    } catch (cause) {
      await refreshOwnerAfterActionError(detail.work_order_id, cause);
    } finally {
      setSaving(false);
    }
  }

  async function submitQaDraft(event: FormEvent<HTMLFormElement>, requestEventId: string) {
    event.preventDefault();
    const qaProfile = profiles.find((profile) => profile.project_id === projectId && profile.kind === 'builtin' && profile.name === 'QA');
    if (!detail || !qaProfile || !detail.events.some((item) => item.id === requestEventId &&
      item.event_type === 'qa_review_requested' && item.run_id === detail.id)) return;
    const title = qaDraftFields[requestEventId]?.title.trim() ?? '';
    const instructions = qaDraftFields[requestEventId]?.instructions.trim() ?? '';
    if (!title || !instructions) return;
    setSaving(true);
    setActionError(null);
    setNotice(null);
    try {
      const created = await draftQaReviewWorkOrder(projectId, detail.id, requestEventId, { title, instructions });
      const saved = (await listWorkOrders(projectId)).find((item) => item.id === created.id);
      if (!saved || saved.project_id !== projectId || saved.profile_id !== qaProfile.id || saved.status !== 'draft' ||
        saved.title !== title || saved.instructions !== instructions) {
        throw new ApiError('The action returned, but QA draft readback did not confirm it. Check Work orders before retrying.', null);
      }
      setQaDrafts((current) => ({ ...current, [requestEventId]: saved }));
      setQaDraftFields((current) => { const next = { ...current }; delete next[requestEventId]; return next; });
      setNotice(`QA draft saved: ${saved.title}. Open Work orders to review it. No run was started.`);
    } catch (cause) {
      setActionError(`${errorMessage(cause)} Check Work orders before retrying.`);
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="activity-workspace" aria-labelledby="activity-heading">
      <div className="view-heading"><div><p className="eyebrow">Work orders / Activity</p><h1 id="activity-heading">Activity</h1>
        <p>Runs and their saved outcomes.</p></div>
        {scopeOrderId && <div className="activity-scope-actions">
          {onOpenTask && <button className="button button-outline" type="button" onClick={() => onOpenTask(scopeOrderId)}>← Task</button>}
          <button className="button button-quiet" type="button" onClick={() => { setScopeOrderId(null); setMobileDetailOpen(false); }}>All activity</button>
        </div>}</div>
      <form className="run-search" role="search" onSubmit={(event) => { event.preventDefault(); setSearch(query); }}>
        <label htmlFor="run-query">Search runs</label>
        <input id="run-query" maxLength={120} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search title, instructions, or event type" />
        <button className="button button-outline" type="submit">Search</button>
        {search && <button className="button button-quiet" type="button" onClick={() => { setQuery(''); setSearch(''); }}>Clear</button>}
      </form>
      {notice && <div className="notice" role="status">{notice}</div>}
      {loading && <div className="loading-panel" role="status"><span className="loading-orb" />Loading runs…</div>}
      {error && !loading && <div className="empty-inline"><p role="alert">{error}</p><button className="button button-outline" type="button" onClick={() => setReload((value) => value + 1)}>Try again</button></div>}
      {!loading && !error && visibleRuns.length === 0 && <div className="empty-inline">{scopeOrderId ? 'No activity for this task yet.' : search ? 'No runs matched this search.' : 'No runs have been recorded for this project.'}</div>}
      {!loading && !error && visibleRuns.length > 0 && <div className={`work-layout ${mobileDetailOpen ? 'is-detail-open' : ''}`}>
        <div className="work-list" aria-label="Runs">{visibleRuns.map((run) =>
          <button key={run.id} type="button" className={`work-list-item ${selectedId === run.id ? 'selected' : ''}`}
            onClick={() => { setSelectedId(run.id); setMobileDetailOpen(true); setNotice(null); }}>
            <span className="run-list-head"><span className="run-list-mode">{run.mode === 'external_code' && externalDetail?.run.id === run.id &&
              externalDetail.attempts.some((attempt) => attempt.target_id === 'github_snapshot_codex_fixture_v1') ? 'Managed synthetic Codex CLI' :
              run.mode === 'external_code' && externalDetail?.run.id === run.id &&
              externalDetail.attempts.some((attempt) => attempt.adapter_id === 'offline_probe') ? 'Offline local source probe' : modeName(run.mode)}</span><span className={`status-chip status-${run.status}`}>{run.status}</span></span>
            <strong>{run.work_order_title ?? 'Historical title unavailable'}</strong>
            <small>{profiles.find((profile) => profile.id === run.profile_id)?.name ?? 'Previous agent'} · {time(run.started_at)}</small>
            {run.status !== 'running' && <span className="run-list-reason">{run.outcome_reason || 'No final reason recorded'}</span>}
          </button>)}</div>
        <div className="run-detail panel">
          <button className="activity-mobile-back button button-quiet" type="button" onClick={() => setMobileDetailOpen(false)}>← Activity list</button>
          {detailLoading && <p className="quiet-note" role="status">Loading run details…</p>}
          {actionError && <div className="form-error" role="alert">{actionError}
            {!detail && !detailLoading && <button className="button button-outline" type="button" onClick={() => setReload((value) => value + 1)}>Retry run readback</button>}
          </div>}
          {detail && !detailLoading && <>
            <div className="detail-top"><span className="eyebrow">Saved run outcome</span><span className={`status-chip status-${detail.status}`}>{detail.status}</span></div>
            <h2>{detail.work_order_title ?? 'Historical title unavailable'}</h2>
            <WorkOrderTrigger cause={detail.triggered_by} />
            {onOpenTask && <button className="activity-open-task button button-outline" type="button" onClick={() => onOpenTask(detail.work_order_id)}>Open task →</button>}
            <section className="run-outcome" aria-label="Selected run outcome">
              <div className="run-outcome-grid">
                <div><span>Mode</span><strong>{detail.mode === 'external_code' && externalDetail?.attempts.some((attempt) => attempt.target_id === 'github_snapshot_codex_fixture_v1') ? 'Managed synthetic Codex CLI' :
                  detail.mode === 'external_code' && externalDetail?.attempts.some((attempt) => attempt.adapter_id === 'offline_probe') ? 'Offline local source probe' : modeName(detail.mode)}</strong></div>
                <div><span>Recorded status</span><strong>{readable(detail.status)}</strong></div>
                <div><span>Last assigned profile</span><strong>{detail.profile_snapshots.at(-1)?.profile_name ?? 'Previous agent · historical name unavailable'}</strong></div>
                <div><span>Saved result evidence</span><strong>{savedResult(detail, textOutputs, liveOutput, codeDetail, externalDetail, operatorResults)}</strong></div>
                <div><span>Usage</span><strong>{detail.mode === 'live_text' ? `${tokens(detail.tokens_used)} · ${liveCost(liveOutput)}` :
                  detail.mode === 'synthetic_text' || detail.mode === 'synthetic_code' ? `Synthetic fixture · ${detail.tokens_used === null ? 'Usage unavailable' : `${detail.tokens_used} provider tokens`} · ${detail.cost_usd === null ? 'Cost not available' : `$${detail.cost_usd.toFixed(4)} provider cost`}` :
                  detail.tokens_used === 0 && detail.cost_usd === 0 ? 'No model usage · 0 tokens · $0.00' : `${tokens(detail.tokens_used)} · ${detail.cost_usd === null ? 'Cost not available' : `$${detail.cost_usd.toFixed(4)}`}`}</strong></div>
              </div>
              <p className="run-outcome-reason"><strong>Outcome reason:</strong> {detail.status === 'running' ? 'Run still in progress' : detail.outcome_reason || 'No final reason recorded'}</p>
              {detail.mode === 'synthetic_code' && <a href="#code-panel-heading" className="run-outcome-link">Inspect saved tests, diff, and artifacts</a>}
              {detail.mode === 'external_code' && <a href="#external-code-heading" className="run-outcome-link">Inspect saved {externalDetail?.attempts.some((attempt) => attempt.target_id === 'github_snapshot_codex_fixture_v1') ? 'synthetic snapshot coding' : externalDetail?.attempts.some((attempt) => attempt.adapter_id === 'offline_probe') ? 'offline probe' : 'coding'} attempt and artifacts</a>}
              {detail.mode === 'manual_planning' && operatorResults?.map((result) =>
                <p className="run-outcome-reason" key={result.id}><strong>Operator result:</strong> {result.summary}</p>)}
            </section>
            <details className="activity-detail-group"><summary>Work order brief and run details</summary>
            <p className="detail-text">{detail.work_order_instructions ?? 'Historical task instructions unavailable.'}</p>
            <div className="detail-facts">

              <div><span>Configured model</span><strong>{detail.mode === 'external_code' ? externalDetail?.attempts.some((attempt) => attempt.target_id === 'github_snapshot_codex_fixture_v1') ? 'Fixed fake model · Engineer profile model ignored' : externalDetail?.attempts.some((attempt) => attempt.adapter_id === 'offline_probe') ? 'None · offline local check' : 'Coding backend shown in the saved attempt below' : detail.mode === 'live_text' ? (liveOutput?.model_id ?? detail.profile_model ?? 'Unavailable') : detail.profile_snapshots.length === 0 ? 'Historical model unavailable' : detail.profile_model ? `${detail.profile_model} (not called)` : 'None'}</strong></div>
              <div><span>Started</span><strong>{time(detail.started_at)}</strong></div>
              <div><span>Finished</span><strong>{detail.finished_at ? time(detail.finished_at) : 'Not recorded'}</strong></div>
            </div>
            </details>
            <details className="activity-detail-group"><summary>Profile, model, and procedure history</summary>
            {detail.mode === 'live_text' && <InferenceLogPanel run={detail} output={liveOutput} />}
            <section className="timeline" aria-labelledby="profile-history-heading"><div className="section-heading"><div><p className="eyebrow">Saved at assignment</p><h3 id="profile-history-heading">Profile history</h3></div></div>
              {detail.profile_snapshots.length === 0 && <p className="quiet-note">Exact profile settings were not recorded for this older run.</p>}
              {detail.profile_snapshots.map((snapshot) => <article className="timeline-step" key={snapshot.transition_index}>
                <div className="timeline-step-body"><h4>{snapshot.profile_name}</h4>
                  <p>Model: {snapshot.profile_model ?? 'None'} · {time(snapshot.recorded_at)}</p>
                  <p className="detail-text">Instructions: {snapshot.profile_instructions || 'None'}</p>
                  <p>Memory: {snapshot.memory_scope} · Tools: {snapshot.allowed_tools.map((tool) => tool.replace(/_v[0-9]+$/, '').replaceAll('_', ' ')).join(', ') || 'None'} · Autonomy: {snapshot.autonomy_level}</p></div>
              </article>)}
            </section>
            {detail.skill_snapshots.length > 0 && <section className="timeline" aria-label={detail.mode === 'live_text' ? 'Procedures supplied to model' : 'Saved profile procedure versions'}>
              <div className="section-heading"><div><p className="eyebrow">Saved at assignment</p><h3>{detail.mode === 'live_text' ? 'Procedures supplied to model' : 'Saved profile procedure versions'}</h3></div></div>
              {detail.mode !== 'live_text' && <p className="quiet-note">These versions were saved with the run. This record does not show whether an agent executed the procedures.</p>}
              {detail.skill_snapshots.map((snapshot) => <article className="timeline-step" key={`${snapshot.transition_index}-${snapshot.skill_id}`}>
                <div className="timeline-step-body"><h4>{snapshot.skill_name} · v{snapshot.version}</h4>
                  <p>{detail.mode === 'live_text' ? `${liveSkillSource(detail, snapshot)} · ` : ''}Assignment {snapshot.transition_index} · {time(snapshot.created_at)}</p>
                  </div>
              </article>)}
            </section>}
            </details>
            <p className="quiet-note">{detail.mode === 'external_code' ?
              externalDetail?.attempts.some((attempt) => attempt.target_id === 'github_snapshot_codex_fixture_v1') ?
                snapshotCodeStatus(externalDetail) :
              externalDetail?.attempts.some((attempt) => attempt.adapter_id === 'offline_probe') ?
                'The offline probe checks a saved local source revision. No coding provider was called. The saved attempt and receipt determine its outcome. No merge, publish, or deployment follows.' :
                'The selected external coding CLI runs in a disposable fixture repository. The saved attempt and receipt determine its outcome. No merge, publish, or deployment follows from this run.' : detail.mode === 'synthetic_code' ?
              'Engineer and QA use a controlled synthetic fixture in a disposable local repository. Queued jobs await saved runner receipts. No model provider or external repository was called.' : detail.mode === 'synthetic_text' ?
              'Researcher and GTM used a local scripted model through the Agents SDK. This is fixture output, not a live AI result. Sales may continue with a local draft and an inbox-approved Mailpit delivery. No provider or web search was called.' :
              detail.mode === 'live_text' ? (detail.status === 'done' && liveOutput
                ? 'The provider returned text and OneFoundry saved it. Done confirms the model response, not completion of the requested deliverable. Check saved actions and artifacts before treating the work order as finished.'
                : 'The live provider run has no confirmed saved text output. Inspect its outcome and events before any new attempt.') :
              'This run records operator-led work. It does not invoke a model or agent. An approved browser inspection can be dispatched separately.'}</p>
            {detail.mode === 'live_text' && detail.status === 'done' && <section className="text-outputs" aria-labelledby="live-output-heading">
              <div className="section-heading"><div><p className="eyebrow">Saved provider result</p><h3 id="live-output-heading">Live text output</h3></div></div>
              {!liveOutput ? <p className="empty-inline">No saved provider output was returned for this run.</p> : <article className="text-output">
                <div className="text-output-head"><div><span className="eyebrow">{liveOutput.provider} · {liveOutput.model_id}</span><h4>Generated text</h4></div><time dateTime={liveOutput.created_at}>{time(liveOutput.created_at)}</time></div>
                <p className="text-output-body">{liveOutput.text}</p>
                {liveOutput.automation_decision && !['none', 'clarify'].includes(liveOutput.automation_decision.status) &&
                  <p className="quiet-note">Automation decision: {liveOutput.automation_decision.status}
                    {liveOutput.automation_decision.rule_version ? ` · rule version ${liveOutput.automation_decision.rule_version}` : ''}
                    {liveOutput.automation_decision.project_version ? ` · project version ${liveOutput.automation_decision.project_version}` : ''}.</p>}
                <p className="quiet-note">{tokenDetail('Input', liveOutput.input_tokens)} · {tokenDetail('Output', liveOutput.output_tokens)} · {liveCost(liveOutput)}</p>
                <div className="output-citations"><strong>Memory supplied to the model</strong>
                  {liveOutput.source_memories.length === 0 ? <p>No approved memory supplied.</p> : <ol>{liveOutput.source_memories.map((source) => <li key={source.id}>
                    <p>{source.text}</p><small>{source.scope} memory · reviewed source</small>
                  </li>)}</ol>}
                </div>
              </article>}
            </section>}
            {detail.mode === 'synthetic_code' && <CodeRunPanel projectId={projectId} runId={detail.id}
              onTimelineRefresh={() => setReload((value) => value + 1)} onDetailChange={setCodeDetail} />}
            {detail.mode === 'external_code' && <ExternalCodePanel projectId={projectId} runId={detail.id}
              onTimelineRefresh={() => setReload((value) => value + 1)} onDetailChange={setExternalDetail} />}
            {detail.events.some((event) => event.run_id === detail.id && event.event_type === 'role_tool_started' &&
              event.payload.tool_id === 'write_text_artifact_v1') &&
              <WorkspaceArtifactPanel key={`${projectId}:${detail.id}`} projectId={projectId} runId={detail.id} />}
            {detail.mode === 'synthetic_text' && <section className="text-outputs" aria-labelledby="text-outputs-heading">
              <div className="section-heading"><div><p className="eyebrow">Persisted fixture results</p><h3 id="text-outputs-heading">Researcher → GTM text</h3></div><span>{textOutputs.length} outputs</span></div>
              {textOutputs.length === 0 && <p className="empty-inline">No text outputs were saved for this run.</p>}
              {textOutputs.map((output) => <article className="text-output" key={output.id}>
                <div className="text-output-head"><div><span className="eyebrow">{output.role} · Synthetic scripted SDK</span><h4>{output.role === 'Researcher' ? 'Research result' : 'GTM draft'}</h4></div>
                  <time dateTime={output.created_at}>{time(output.created_at)}</time></div>
                <p className="text-output-body">{output.text}</p>
                <p className="quiet-note">Configured model: {output.configured_model ? `${output.configured_model} (not called)` : 'None'} · {output.tokens_used} provider tokens · ${output.cost_usd.toFixed(4)} provider cost</p>
                <div className="output-citations"><strong>Source memory citations</strong>
                  {output.source_memories.length === 0 ? <p>No approved memory cited.</p> :
                    <ol>{output.source_memories.map((source) => <li key={source.id}>
                      <p>{source.text}</p><small>{source.scope} memory · reviewed source</small>
                    </li>)}</ol>}
                </div>
              </article>)}
            </section>}
            {detail.mode === 'synthetic_text' && <SalesPanel projectId={projectId} runId={detail.id}
              onChanged={() => setReload((value) => value + 1)} />}
            <BrowserStaticPanel key={`${projectId}:${detail.id}`} projectId={projectId} runId={detail.id} events={detail.events} />
            {detail.mode === 'manual_planning' && detail.status === 'running' && ownerReadError &&
              <div className="form-error" role="alert">Could not confirm the current work order state: {ownerReadError} Manual actions are unavailable until it can be checked.
                <button className="button button-outline" type="button" onClick={() => setReload((value) => value + 1)}>Refresh run and work order</button>
              </div>}
            {detail.mode === 'manual_planning' && detail.status === 'running' && currentOrder?.status === 'blocked' &&
              <div className="work-blocked-detail" role="status"><strong>Work order blocked</strong><span>{currentOrder.block_reason}</span>
                <small>Resume this work order from Work orders before recording a handoff or outcome.</small></div>}
            {detail.mode === 'manual_planning' && detail.status === 'running' && currentOrder && currentOrder.status !== 'running' && currentOrder.status !== 'blocked' &&
              <p className="quiet-note" role="status">This work order is {currentOrder.status}. Refresh the saved state before recording another action.</p>}
            {detail.mode === 'manual_planning' && detail.status === 'running' && currentOrder?.status === 'running' && <div className="run-controls">
              <form onSubmit={(event) => { void submitHandoff(event); }}>
                <h3>Handoff</h3><p>Change the profile assigned to the manual run.</p>
                <label className="field"><span className="field-label">Next profile</span><select required value={handoffTo} onChange={(event) => setHandoffTo(event.target.value)}>
                  <option value="">Choose a profile</option>{profiles.filter((profile) => profile.id !== detail.profile_id).map((profile) =>
                    <option key={profile.id} value={profile.id}>{profile.name}</option>)}</select></label>
                <button className="button button-outline" type="submit" disabled={saving || profiles.length < 2}>Record handoff</button>
              </form>
              <form onSubmit={(event) => { void submitFinish(event); }}>
                <h3>Finish planning run</h3><p>Record an outcome and reason. No external action follows.</p>
                <label className="field"><span className="field-label">Outcome</span><select value={outcome} onChange={(event) => setOutcome(event.target.value as Exclude<RunStatus, 'running'>)}>
                  <option value="done">Done</option><option value="failed">Failed</option><option value="unknown">Unknown</option></select></label>
                <label className="field"><span className="field-label">Reason</span><textarea required maxLength={2000} value={reason} onChange={(event) => setReason(event.target.value)} rows={2} /></label>
                <button className="button button-outline" type="submit" disabled={saving}>Record outcome</button>
              </form>
            </div>}
            <section className="timeline" aria-labelledby="steps-heading"><div className="section-heading"><div><p className="eyebrow">Owner record</p><h3 id="steps-heading">Steps</h3></div><span>{detail.events.length} events</span></div>
              {detail.events.length === 0 && <p className="empty-inline">No events were returned for this run.</p>}
              {detail.events.map((step) => <article key={step.id} className="timeline-step">
                <span className="timeline-marker" aria-hidden="true" /><div className="timeline-step-body"><div className="timeline-step-head"><h4>{describeActivity(step).title}</h4><time dateTime={step.created_at}>{time(step.created_at)}</time></div>
                  <p>{describeActivity(step).detail}</p>

                  {step.event_type === 'qa_review_requested' && step.run_id === detail.id &&
                    (qaDrafts[step.id] ? <p className="quiet-note">QA draft “{qaDrafts[step.id].title}” is saved. Open Work orders to review it.</p> :
                      <form onSubmit={(event) => { void submitQaDraft(event, step.id); }}>
                        <p>Create an operator written QA work order for this request. It stays in draft.</p>
                        <label className="field"><span className="field-label">QA draft title</span>
                          <input required maxLength={160} value={qaDraftFields[step.id]?.title ?? ''} onChange={(event) => setQaDraftFields((current) => ({ ...current,
                            [step.id]: { title: event.target.value, instructions: current[step.id]?.instructions ?? '' } }))} /></label>
                        <label className="field"><span className="field-label">QA draft instructions</span>
                          <textarea required maxLength={10000} rows={3} value={qaDraftFields[step.id]?.instructions ?? ''} onChange={(event) => setQaDraftFields((current) => ({ ...current,
                            [step.id]: { title: current[step.id]?.title ?? '', instructions: event.target.value } }))} /></label>
                        <button className="button button-outline" type="submit" disabled={saving || !profiles.some((profile) => profile.project_id === projectId && profile.kind === 'builtin' && profile.name === 'QA')}>Create QA draft</button>
                      </form>)}
                </div>
              </article>)}
            </section>
          </>}
        </div>
      </div>}
    </section>
  );
}
