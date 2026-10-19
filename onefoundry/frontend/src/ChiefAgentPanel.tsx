import { useEffect, useRef, useState } from 'react';
import { getTeamSession, type TeamAssignment, type TeamSessionView } from './taskSessionApi';
import type { Profile } from './api';
import { ChiefConversationText, isPublicAgentMessage } from './ChiefConversationText';
import { WorkspaceArtifactPanel } from './WorkspaceArtifactPanel';
import { ChiefAgentRecovery } from './ChiefAgentRecovery';
import { TaskSessionActivity } from './TaskSessionActivity';
import { messagePresentation } from './taskSessionActivity';
import { ChiefArtifactMessageText } from './ChiefArtifactMessageText';

export type ChiefFile = { id: string; run_id: string; profile_id: string; profile_name: string; kind: string;
  work_order_title?: string | null; sha256?: string; status?: 'ready' | 'pending' | 'unknown' };
type Props = { projectId: string; orderId: string; sessionId: string; main: TeamSessionView;
  profiles: Profile[]; files: ChiefFile[]; initialTab?: 'context' | 'conversation'; onClose: () => void };
export function ChiefAgentPanel({ projectId, orderId, sessionId, main, profiles, files, initialTab = 'context', onClose }: Props) {
  const [tab, setTab] = useState<'context' | 'conversation' | 'files'>(initialTab);
  const [view, setView] = useState<TeamSessionView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [activityOpen, setActivityOpen] = useState(false);
  const panel = useRef<HTMLElement>(null);
  const tabs = useRef<(HTMLButtonElement | null)[]>([]);
  const child = main.children.find((item) => item.id === sessionId);
  const profile = profiles.find((item) => item.id === child?.profile_id);
  const assignment: TeamAssignment | undefined = main.assignments.find((item) => item.child_session_id === sessionId);
  const reviewFeedback = initialTab === 'conversation' ? [...(view?.messages ?? [])].reverse().find((item) => {
    const presentation = messagePresentation(item.body);
    return item.kind === 'agent' && presentation.kind === 'text' && presentation.record?.verdict === 'needs_rework';
  }) : undefined;
  const conversationMessages = view?.messages.filter((item) => item.id !== reviewFeedback?.id && item.kind !== 'system' &&
    (item.kind.includes('operator') || isPublicAgentMessage(item.body))) ?? [];
  const latestAttempt = view?.attempts.at(-1);
  async function refresh() {
    const saved = await getTeamSession(projectId, orderId, sessionId);
    setView(saved);
    return saved;
  }
  useEffect(() => {
    const controller = new AbortController();
    setView(null); setError(null);
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    panel.current?.querySelector<HTMLButtonElement>('button')?.focus();
    void getTeamSession(projectId, orderId, sessionId, controller.signal).then((saved) => {
      if (!controller.signal.aborted) setView(saved);
    }).catch((cause: unknown) => { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : 'Agent details unavailable.'); });
    return () => { controller.abort(); previous?.focus(); };
  }, [projectId, orderId, sessionId]);
  const names = ['context', 'conversation', 'files'] as const;
  return <div className="chief-agent-overlay"><button className="chief-agent-backdrop" aria-label="Return to Chief" onClick={onClose} />
    <aside className="chief-agent-panel" ref={panel} role="dialog" aria-modal="true" aria-labelledby="chief-agent-name" onKeyDown={(event) => {
      if (event.key === 'Escape') onClose();
      if (event.key === 'Tab') {
        const focusable = [...(panel.current?.querySelectorAll<HTMLElement>('button:not(:disabled),a[href],input,textarea,summary,[tabindex="0"]') ?? [])];
        if (event.shiftKey && document.activeElement === focusable[0]) { event.preventDefault(); focusable.at(-1)?.focus(); }
        else if (!event.shiftKey && document.activeElement === focusable.at(-1)) { event.preventDefault(); focusable[0]?.focus(); }
      }
    }}>
      <header><button onClick={onClose}>← Chief</button><h2 id="chief-agent-name">{profile?.name ?? 'Agent'}</h2></header>
      <div className="chief-panel-tabs" role="tablist" aria-label="Agent details">{names.map((name, index) => <button key={name} ref={(element) => { tabs.current[index] = element; }} role="tab" id={`chief-agent-tab-${name}`} aria-controls={`chief-agent-${name}`} aria-selected={tab === name} tabIndex={tab === name ? 0 : -1}
        onClick={() => setTab(name)} onKeyDown={(event) => {
          if (!['ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(event.key)) return;
          event.preventDefault(); const next = event.key === 'Home' ? 0 : event.key === 'End' ? 2 : (index + (event.key === 'ArrowRight' ? 1 : 2)) % 3;
          setTab(names[next]!); tabs.current[next]?.focus();
        }}>{name === 'context' ? 'Context' : name === 'conversation' ? 'Conversation' : 'Files'}</button>)}</div>
      {view && <ChiefAgentRecovery projectId={projectId} orderId={orderId} view={view} onChanged={refresh} />}
      <section role="tabpanel" id={`chief-agent-${tab}`} aria-labelledby={`chief-agent-tab-${tab}`}>
        {error && <p role="alert">{error}</p>}
        {tab === 'context' && <><h3>Assignment</h3><ChiefConversationText body={assignment?.objective ?? 'Historical assignment; inspect the saved conversation.'} />
          {assignment && <><h3>Expected result</h3><ChiefConversationText body={assignment.expected_result} /><h3>Checks</h3><ChiefConversationText body={assignment.acceptance_check} /><p>Assignment: {assignment.status === 'blocked' ? 'Stopped; Chief coordinates recovery' : assignment.status}</p></>}
          {view?.messages.filter((item) => item.kind === 'system').map((item) => <section key={item.id}><h3>Supplied context</h3><ChiefConversationText body={item.body} /></section>)}
          <p>Chief owns this assignment and its next step.</p></>}
        {tab === 'conversation' && <>{!view && !error && <p role="status">Reading saved conversation…</p>}
          {reviewFeedback && <section aria-label="Review feedback"><h3>Latest changes requested</h3><ChiefArtifactMessageText body={reviewFeedback.body} projectId={projectId} files={files} /></section>}
          {view && !reviewFeedback && conversationMessages.length === 0 && !view.history_omitted?.messages &&
            <p role="status">{latestAttempt?.status === 'failed'
              ? latestAttempt.reason === 'memory_embedding_timeout'
                ? 'Memory lookup timed out before the agent started. Chief can review the recorded attempt.'
                : 'The agent stopped before saving a reply. Chief can review the recorded attempt.'
              : 'No conversation reply has been saved yet. Review recorded actions and earlier attempts for the current state.'}</p>}
          {conversationMessages.map((item) => <article className="chief-agent-message" key={item.id}><small>{item.kind.includes('operator') ? 'Operator' : profile?.name ?? 'Agent'}</small>{item.kind.includes('operator')
            ? <ChiefConversationText body={item.body} /> : <ChiefArtifactMessageText body={item.body} projectId={projectId} files={files} />}</article>)}
          {!!view?.history_omitted?.messages && <p>Earlier messages remain available in Details.</p>}
          {view && <details className="chief-agent-activity" onToggle={(event) => setActivityOpen(event.currentTarget.open)}><summary>Recorded actions and earlier attempts</summary>
            {activityOpen && <TaskSessionActivity view={view} profiles={profiles} />}</details>}</>}
        {tab === 'files' && <>{[...new Set(files.filter((item) => item.profile_id === child?.profile_id && view?.attempts.some((attempt) => attempt.id === item.run_id)).map((item) => item.run_id))].map((runId) => <WorkspaceArtifactPanel key={runId} projectId={projectId} runId={runId} />)}
          {!view && !error && <p role="status">Reading saved files…</p>}
          {view && !files.some((item) => item.profile_id === child?.profile_id && view.attempts.some((attempt) => attempt.id === item.run_id)) && <p>No saved files yet.</p>}</>}
      </section>
    </aside></div>;
}
