import { useEffect, useRef, useState, type FormEvent } from 'react';

import { ApiError, type Profile } from './api';
import { TaskSessionActivity } from './TaskSessionActivity';
import { MediaCapturePanel } from './MediaCapturePanel';
import { useConversationFollow } from './useConversationFollow';
import { safeActivityText } from './taskSessionActivity';
import { acknowledgeUnknown, continueTeamSession, getTeamSession, reconcileTeamActions, returnTeamChild, sendTeamMessage,
  type TeamAssignment, type TeamAttempt, type TeamSessionView } from './taskSessionApi';

type Props = { projectId: string; orderId: string; profiles: Profile[]; reloadKey: number; onOpenRun?: (runId: string) => void };
type Pending = { kind: 'message' | 'continue' | 'rework'; sessionId: string; id: string; sequence?: number; textHash?: string; resultMessageId?: string };
const OBVIOUS_SECRET = /(?:\b(?:sk-[A-Za-z0-9_-]{12,}|sk_live_[A-Za-z0-9]{12,}|ghp_[A-Za-z0-9]{12,}|github_pat_[A-Za-z0-9_]{12,}|xoxb-[A-Za-z0-9-]{12,}|AKIA[A-Z0-9]{16})\b|-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*\S+|\bBearer\s+[A-Za-z0-9._~-]{12,})/i;

function pendingKey(projectId: string, orderId: string): string {
  return `onefoundry:team-session-pending:${projectId}:${orderId}`;
}

function selectedKey(projectId: string, orderId: string): string {
  return `onefoundry:team-session-selected:${projectId}:${orderId}`;
}

function readSelected(projectId: string, orderId: string): string | null {
  try { return window.sessionStorage.getItem(selectedKey(projectId, orderId)); }
  catch { return null; }
}

function readPending(projectId: string, orderId: string): Pending | null {
  try {
    const raw = window.sessionStorage.getItem(pendingKey(projectId, orderId));
    if (!raw) return null;
    const value: unknown = JSON.parse(raw);
    if (!value || typeof value !== 'object') return null;
    const item = value as Record<string, unknown>;
    if ((item.kind !== 'message' && item.kind !== 'continue' && item.kind !== 'rework') ||
        typeof item.sessionId !== 'string' || typeof item.id !== 'string' ||
        item.kind !== 'continue' && (typeof item.textHash !== 'string' || !Number.isSafeInteger(item.sequence)) ||
        item.kind === 'rework' && typeof item.resultMessageId !== 'string') return null;
    return item as Pending;
  } catch { return null; }
}

async function hashText(value: string): Promise<string> {
  const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
  return Array.from(new Uint8Array(bytes), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'The request failed. Check saved state before retrying.';
}

function statusLabel(status: string): string {
  return status.replaceAll('_', ' ');
}

function effort(attempts: TeamAttempt[]): string {
  const reported = attempts.filter((item) => item.input_tokens !== null && item.output_tokens !== null);
  const tokens = reported.reduce((total, item) => total + item.input_tokens! + item.output_tokens!, 0);
  const costs = attempts.map((item) => item.cost_usd === null ? null : Number(item.cost_usd));
  const cost = costs.length && costs.every((value) => value !== null && Number.isFinite(value))
    ? `$${costs.reduce<number>((total, value) => total + (value ?? 0), 0).toFixed(4)} reported USD` : 'USD cost unknown';
  return `${attempts.length} ${attempts.length === 1 ? 'turn' : 'turns'}` +
    `${reported.length ? ` · ${tokens.toLocaleString()} reported tokens (${reported.length}/${attempts.length} turns)` : ''} · ${cost}`;
}

export function TaskSessionView({ projectId, orderId, profiles, reloadKey, onOpenRun }: Props) {
  const [root, setRoot] = useState<TeamSessionView | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(() => readSelected(projectId, orderId));
  const [current, setCurrent] = useState<TeamSessionView | null>(null);
  const [draft, setDraft] = useState('');
  const [editingMessageId, setEditingMessageId] = useState<string | null>(null);
  const [draftBeforeEdit, setDraftBeforeEdit] = useState<string | null>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const [planOpen, setPlanOpen] = useState(false);
  const [pending, setPending] = useState<Pending | null>(() => readPending(projectId, orderId));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const submissionInFlight = useRef(false);
  const rootId = root?.session.id ?? null;
  const activeId = selectedId ?? rootId;
  const transcriptRef = useConversationFollow(activeId);

  function savePending(value: Pending | null): boolean {
    try {
      if (value) window.sessionStorage.setItem(pendingKey(projectId, orderId), JSON.stringify(value));
      else window.sessionStorage.removeItem(pendingKey(projectId, orderId));
      setPending(value);
      return true;
    } catch {
      setError('The browser could not preserve this submission ID. Sending is paused.');
      return false;
    }
  }

  useEffect(() => {
    setSelectedId(readSelected(projectId, orderId)); setRoot(null); setCurrent(null); setDraft(''); setEditingMessageId(null); setDraftBeforeEdit(null);
    setPending(readPending(projectId, orderId)); setError(null); setRefreshError(null);
  }, [projectId, orderId]);

  useEffect(() => {
    const controller = new AbortController();
    let latestRequest = 0;
    const refresh = () => {
      if (document.hidden) return;
      const request = ++latestRequest;
      void getTeamSession(projectId, orderId, undefined, controller.signal).then(async (savedRoot) => {
        if (controller.signal.aborted || request !== latestRequest) return;
        const target = selectedId && savedRoot.children.some((child) => child.id === selectedId) ? selectedId : null;
        if (selectedId && !target) {
          setSelectedId(null);
          try { window.sessionStorage.removeItem(selectedKey(projectId, orderId)); } catch { /* Selection has no execution effect. */ }
        }
        const saved = target ? await getTeamSession(projectId, orderId, target, controller.signal) : savedRoot;
        if (controller.signal.aborted || request !== latestRequest) return;
        setRoot(savedRoot);
        setCurrent(saved);
        setRefreshError(null);
        setPending((prior) => {
          if (!prior) return null;
          const confirmed = prior.sessionId === saved.session.id && (prior.kind !== 'continue'
            ? saved.messages.some((item) => item.client_message_id === prior.id)
            : saved.attempts.some((item) => item.client_attempt_id === prior.id));
          if (confirmed) {
            try { window.sessionStorage.removeItem(pendingKey(projectId, orderId)); } catch { /* Saved readback is authoritative. */ }
            return null;
          }
          return prior;
        });
      }).catch((cause: unknown) => {
        if (!controller.signal.aborted && request === latestRequest) setRefreshError(errorMessage(cause));
      });
    };
    refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [projectId, orderId, selectedId, reloadKey, revision]);

  const session = current?.session;
  const profileName = (id: string) => profiles.find((item) => item.id === id)?.name ?? 'Agent';
  const assignment = root?.assignments.find((item) => item.child_session_id === activeId);
  const activeAttempt = current?.attempts.find((item) => item.status === 'running' || item.status === 'queued');
  const waitingForInput = assignment?.status === 'pending';
  const canWrite = !!session && session.id === activeId && session.execution_mode === 'team' && session.status !== 'closed' &&
    (!session.work_order_status || ['ready', 'running'].includes(session.work_order_status)) &&
    !current?.attempts.some((item) => item.status === 'unknown' && !item.unknown_acknowledged);
  const canRework = !!session && session.id === activeId && session.execution_mode === 'team' &&
    session.status === 'closed' && session.work_order_status === 'needs_review' &&
    assignment?.status === 'satisfied' && !!assignment.result_message_id && ['artifact', 'text_report'].includes(assignment.acceptance_kind ?? '') &&
    !!root && root.session.status !== 'closed' && root.session.execution_mode === 'team' &&
    ['ready', 'running', 'needs_review'].includes(root.session.work_order_status ?? '') &&
    !root.attempts.some((item) => item.status === 'queued' || item.status === 'running' || item.status === 'unknown' && !item.unknown_acknowledged) &&
    !current?.attempts.some((item) => item.status === 'queued' || item.status === 'running' || item.status === 'unknown' && !item.unknown_acknowledged) &&
    !current?.tool_calls.some((item) => item.status === 'started') &&
    !root.children.some((item) => item.status === 'working');
  const canContinue = canWrite && session?.waiting_reason !== 'local_recording' && session?.waiting_reason !== 'recording_unconfirmed' && !activeAttempt && !waitingForInput && !saving && !pending;
  const lastAttempt = current?.attempts.at(-1);
  const uncertainActions = current?.tool_calls.some((item) => item.status === 'started');
  const unacknowledged = current?.attempts.find((item) => item.status === 'unknown' && !item.unknown_acknowledged);
  const [ackReason, setAckReason] = useState('');
  const ackAction = useRef<string | null>(null);
  useEffect(() => { ackAction.current = null; setAckReason(''); }, [activeId]);

  async function abandonUnknown() {
    if (!session || !unacknowledged || activeAttempt || uncertainActions || saving || ackReason.trim().length < 10) return;
    setSaving(true); setError(null);
    try {
      ackAction.current ??= crypto.randomUUID();
      await acknowledgeUnknown(projectId, orderId, session.id, unacknowledged.id, ackAction.current, ackReason.trim());
      setNotice('Unverified outcome abandoned. Its history stays unknown; no turn was launched.');
      ackAction.current = null; setAckReason(''); setRevision((value) => value + 1);
    } catch (cause) { setError(errorMessage(cause)); }
    finally { setSaving(false); }
  }

  async function reconcile() {
    if (!session || saving || activeAttempt) return;
    setSaving(true); setError(null);
    try {
      const count = await reconcileTeamActions(projectId, orderId, session.id);
      setNotice(`${count} saved action receipts reconciled. No agent turn was launched.`);
      setRevision((value) => value + 1);
    } catch (cause) { setError(errorMessage(cause)); }
    finally { setSaving(false); }
  }

  async function submitMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!session || (!canWrite && !canRework) || saving || pending || submissionInFlight.current || !draft.trim()) return;
    const body = draft.trim();
    if (OBVIOUS_SECRET.test(body)) { setError('This message appears to contain a secret. Remove it before sending.'); return; }
    submissionInFlight.current = true;
    try {
      const next: Pending = { kind: canRework ? 'rework' : 'message', sessionId: session.id, id: crypto.randomUUID(),
        resultMessageId: canRework ? assignment!.result_message_id! : undefined,
        sequence: current?.messages.at(-1)?.sequence ?? 0, textHash: await hashText(body) };
      if (savePending(next)) await submitSaved(next, body);
    } catch (cause) {
      setError(`Could not prepare the message: ${errorMessage(cause)}`);
    } finally { submissionInFlight.current = false; }
  }

  async function submitSaved(value: Pending, body?: string) {
    if (!session || value.sessionId !== session.id || saving) return;
    setSaving(true); setError(null); setNotice(null);
    try {
      if (value.kind !== 'continue') {
        if (!body || await hashText(body) !== value.textHash) {
          setError('Re-enter the exact message to retry the saved submission ID.'); return;
        }
        if (value.kind === 'rework') {
          await returnTeamChild(projectId, orderId, session.id, value.id, value.sequence!, value.resultMessageId!, body);
          setDraft(''); setEditingMessageId(null); setDraftBeforeEdit(null);
          setNotice('Revision saved and one new turn queued. Previous results remain saved.');
        } else {
          const result = await sendTeamMessage(projectId, orderId, session.id, value.id, value.sequence!, body);
          setDraft(''); setEditingMessageId(null); setDraftBeforeEdit(null);
          setNotice(result.state === 'waiting_for_input' ? 'Message saved. This agent is waiting for its assigned input.' :
            result.state === 'coalesced' ? 'Message saved. The agent will receive it on its next turn.' : 'Message saved. Agent turn queued.');
        }
      } else {
        await continueTeamSession(projectId, orderId, session.id, value.id);
        setNotice('Continuation queued for this agent.');
      }
      savePending(null);
      setRevision((value) => value + 1);
    } catch (cause) {
      if (value.kind === 'rework' && cause instanceof ApiError && (cause.status === 409 || cause.status === 422)) {
        if (savePending(null)) {
          setNotice(`${errorMessage(cause)} Revision was not queued. Your feedback is preserved; review the refreshed state before submitting again.`);
        }
      } else {
        setError(`${errorMessage(cause)} The original submission ID is preserved; check saved state before retrying.`);
      }
      setRevision((value) => value + 1);
    } finally { setSaving(false); }
  }

  function continueWork() {
    if (!session || !canContinue || submissionInFlight.current) return;
    submissionInFlight.current = true;
    const next: Pending = { kind: 'continue', sessionId: session.id, id: crypto.randomUUID() };
    if (savePending(next)) void submitSaved(next).finally(() => { submissionInFlight.current = false; });
    else submissionInFlight.current = false;
  }

  function chooseSession(id: string) {
    const selected = id === rootId ? null : id;
    setSelectedId(selected);
    try {
      if (selected) window.sessionStorage.setItem(selectedKey(projectId, orderId), selected);
      else window.sessionStorage.removeItem(selectedKey(projectId, orderId));
    } catch { /* Selection has no execution effect; the current view remains usable. */ }
    setCurrent(null);
    setDraft(''); setEditingMessageId(null); setDraftBeforeEdit(null); setNotice(null); setError(null); setRefreshError(null);
  }
  async function copyMessage(text: string) {
    try { await navigator.clipboard.writeText(text); setNotice('Message copied.'); }
    catch { setError('Could not copy the message. Check clipboard access in this browser.'); }
  }
  function editMessage(id: string, text: string) {
    if ((!canWrite && !canRework) || saving || pending) return;
    setDraftBeforeEdit((prior) => prior ?? draft); setDraft(text); setEditingMessageId(id); setError(null); composerRef.current?.focus();
  }

  const visibleAttempts = current?.attempts ?? [];
  const historyOmitted = current?.history_omitted;
  const olderEntries = historyOmitted ? Object.values(historyOmitted).reduce((total, count) => total + count, 0) : 0;
  return <div className="task-chat-workspace task-session-workspace" aria-label="Continuing team session">
    <aside className="task-chat-threads task-session-team is-open" aria-label="Agent sessions">
      <p className="task-chat-threads-heading">Team sessions</p>
      {root && <button type="button" aria-current={!selectedId ? 'page' : undefined}
        onClick={() => chooseSession(root.session.id)}>Chief of Staff · {statusLabel(root.session.status)}</button>}
      {root?.children.map((child) => {
        const item = root.assignments.find((saved) => saved.child_session_id === child.id);
        return <button key={child.id} type="button" aria-current={selectedId === child.id ? 'page' : undefined}
          onClick={() => chooseSession(child.id)}>{profileName(child.profile_id)} · {child.execution_mode === 'legacy' ? 'preserved legacy draft' : statusLabel(child.work_order_status === 'failed' ? 'work_order_failed' : item?.status ?? child.status)}</button>;
      })}
    </aside>
    <div className="task-chat-conversation">
      <div className="task-session-heading">
        <strong>{session ? profileName(session.profile_id) : 'Loading session…'}</strong>
        {selectedId && rootId && <button type="button" onClick={() => chooseSession(rootId)}>← Chief of Staff</button>}
        {current && <small>{historyOmitted?.attempts ? 'Visible turn usage' : 'This agent'}: {effort(visibleAttempts)}</small>}
      </div>
      <div className="task-chat-transcript task-session-transcript" ref={transcriptRef} role="log" aria-label="Saved agent messages and turns">
        {olderEntries > 0 && <p role="status" className="task-chat-guidance">
          Showing recent activity. {olderEntries.toLocaleString()} earlier entries remain saved.
        </p>}
        {assignment && <section className="task-session-assignment"><strong>Assignment</strong>
          <p>{assignment.objective.length > 240 ? `${assignment.objective.slice(0, 240)}…` : assignment.objective}</p>
          <details><summary>Assignment details and completion check</summary>
            {assignment.objective.length > 240 && <p>{assignment.objective}</p>}
            <small>Expected result: {assignment.expected_result}</small>
            <small>Completion check: {assignment.acceptance_check}</small></details></section>}
        {current && <TaskSessionActivity key={current.session.id} view={current} profiles={profiles} onOpenRun={onOpenRun} onOpenSession={chooseSession}
          onCopyMessage={(text) => { void copyMessage(text); }} onEditMessage={!pending && !saving && (canWrite || canRework) ? editMessage : undefined} />}
      </div>
      <div className="task-chat-compose">
        {notice && <p role="status" className="task-chat-guidance">{notice}</p>}
        {error && <p role="alert" className="form-error">{error}</p>}
        {refreshError && <p role="alert" className="form-error">Saved-state refresh failed: {refreshError}</p>}
        {pending && pending.sessionId !== session?.id && <p role="status" className="task-chat-guidance">
          An earlier submission is still unconfirmed in another agent session. Return there to reconcile it before sending more work.
          <button type="button" onClick={() => chooseSession(pending.sessionId)}>Open that agent</button></p>}
        {pending && pending.sessionId === session?.id && <p role="status" className="task-chat-guidance">
          Submission outcome unconfirmed. {pending.kind !== 'continue' ? 'Re-enter the exact message' : 'Retry the same continuation'} using its saved ID.
          <button type="button" disabled={saving || pending.kind !== 'continue' && !draft.trim()}
            onClick={() => { void submitSaved(pending, draft.trim()); }}>Retry same submission</button></p>}
        {session?.status === 'waiting_for_operator' && <p className="task-chat-guidance">
          {session.waiting_reason === 'recording_ready' ? 'Video saved. Open Results to verify its playback, then explicitly continue the requesting agent.' : session.waiting_reason === 'recording_unconfirmed' ? 'The recording storage outcome is unconfirmed. Inspect its saved intent; do not re-upload or replay it.' : session.waiting_reason === 'local_recording' ? 'Waiting for your local recording. Save and verify the video in Results, then explicitly continue the requesting agent.' : assignment && profiles.find((item) => item.id === session.profile_id)?.autonomy_level === 'manual' ?
            'This agent uses manual autonomy. Mark its work order ready in Manage, then start or continue it.' :
            'This agent paused at an automatic safeguard. Reply or Continue work to decide its next step. Other queued agents may still run.'}</p>}
        {activeAttempt?.retry_of_attempt_id && <p className="task-chat-guidance">
          The provider did not accept the last request, so this agent will retry it once shortly.</p>}
        {waitingForInput && <p className="task-chat-guidance">This agent is waiting for its declared input. Your message will be saved for its next turn.</p>}
        {lastAttempt && (lastAttempt.status === 'failed' || lastAttempt.status === 'unknown') &&
          <p className="task-chat-guidance">{lastAttempt.status === 'unknown' ? 'Outcome unknown' : 'Attempt failed'}: {lastAttempt.reason
            ? safeActivityText(lastAttempt.reason).replaceAll('_', ' ').slice(0, 240) : 'No stop reason recorded.'} Confirmed work remains saved.</p>}
        {session?.status === 'closed' && <p className="task-chat-guidance">{canRework
          ? 'Describe the revision and include its required source text. Returning this assignment keeps previous results and queues one new turn.'
          : 'This assignment is closed. Ask the Chief of Staff for a new revision if more work is needed.'}</p>}
        {uncertainActions && !activeAttempt && <button className="button button-outline" type="button" disabled={saving}
          onClick={() => { void reconcile(); }}>Reconcile saved actions</button>}
        {unacknowledged && !activeAttempt && <fieldset disabled={saving || uncertainActions}>
          <legend>Unknown outcome needs your decision</legend>
          <p>Reconcile saved actions first. Abandoning the unverified result preserves the unknown history and permits separate work; it does not confirm success or replay the attempt.</p>
          <label>Reason for abandoning the unverified result
            <textarea value={ackReason} maxLength={1000} onChange={(event) => { setAckReason(event.target.value); ackAction.current = null; }} /></label>
          <button type="button" disabled={ackReason.trim().length < 10} onClick={() => { void abandonUnknown(); }}>Abandon unverified result</button>
        </fieldset>}
        {profiles.some((profile) => profile.allowed_tools.includes('request_video_capture_v1')) &&
          <details className="task-session-capture-tools"><summary>Local video requests and capture</summary><MediaCapturePanel projectId={projectId} orderId={orderId} canRecord={!!root?.session.work_order_status && ['ready', 'running'].includes(root.session.work_order_status)}
            reloadKey={`${root?.children.map((item) => item.id + item.status).join('|')}-${lastAttempt?.id}-${lastAttempt?.status}`} /></details>}
        {canContinue && !unacknowledged && <button className="button button-outline" type="button" onClick={continueWork}>
          {current?.attempts.length ? `Continue work with ${profileName(session!.profile_id)}` : `Start work with ${profileName(session!.profile_id)}`}</button>}
        {!canRework && <p className="task-chat-guidance">This team session is active. Send to agent queues a new work turn.</p>}
        {editingMessageId && <p role="status" className="task-chat-guidance">Revising earlier text. Send saves a new message; the original remains in this session. <button type="button" onClick={() => { setDraft(draftBeforeEdit ?? ''); setEditingMessageId(null); setDraftBeforeEdit(null); }}>Cancel edit</button></p>}
        <form onSubmit={(event) => { void submitMessage(event); }}>
          <div className="task-chat-compose-control">
            <label className="task-chat-compose-label" htmlFor="team-session-draft">{canRework ? 'Revision for' : 'Message to'} {session && profileName(session.profile_id)}</label>
            <textarea id="team-session-draft" ref={composerRef} value={draft} onChange={(event) => setDraft(event.target.value)}
              rows={2} placeholder={`Message ${session ? profileName(session.profile_id) : 'agent'}…`}
              disabled={(!canWrite && !canRework) || saving || !!pending && pending.kind === 'continue'} />
            <button className="button button-primary task-chat-send" type="submit"
              disabled={(!canWrite && !canRework) || saving || !draft.trim() || canRework && draft.trim().length < 10 || !!pending}>{canRework ? 'Return for rework' : 'Send to agent'}</button>
          </div>
        </form>
      </div>
    </div>
    <button type="button" className="task-session-plan-toggle" aria-expanded={planOpen}
      onClick={() => setPlanOpen((value) => !value)}>{planOpen ? 'Hide team plan' : 'Team plan and results'}</button>
    <aside className={`task-chat-context task-session-plan ${planOpen ? 'is-open' : ''}`} aria-label="Team plan and results">
      <h2>Team plan</h2>
      {root?.decision_causes.filter((item) => item.dispatch_attempt_id === null && item.resolved_at === null).map((item) =>
        <section key={item.id} className="task-session-decision">
          <strong>{item.kind === 'blocker' ? 'Needs Chief decision' : 'Ready for Chief review'}</strong>
          <p>{item.detail}</p>
        </section>)}
      {root?.assignments.length ? root.assignments.map((item: TeamAssignment) => <section key={item.id}>
        <strong>{profileName(root.children.find((child) => child.id === item.child_session_id)?.profile_id ?? '')} · {statusLabel(root.children.find((child) => child.id === item.child_session_id)?.work_order_status === 'failed' ? 'work_order_failed' : item.status)}</strong>
        <p>{item.objective}</p><small>{item.result_message_id ? (item.status === 'satisfied' ? 'Result saved' : 'Previous result preserved; revision pending') : `Check: ${item.acceptance_check}`}</small>
      </section>) : <p>No assignments saved yet. A proposed plan is not an assigned task.</p>}
      <details><summary>Turn details and usage</summary><p>This agent: {effort(visibleAttempts)}</p>
        <p>Messages and confirmed assignments are shown in their agent sessions. Private reasoning is not recorded.</p></details>
    </aside>
  </div>;
}
