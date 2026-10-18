import type { ChiefProgress } from './chiefProgressApi';

export function ChiefProgressCard({ progress, error, status, onOpenAgent, onMessageChief, onReviewApproval, onReviewDeliverables, onReviewOutcome, onResume, resumeDisabled }: {
  progress: ChiefProgress | null; error: string | null; status: string;
  onOpenAgent: (id: string) => void; onMessageChief: () => void;
  onReviewApproval: () => void; onReviewDeliverables: () => void; onReviewOutcome: () => void;
  onResume?: () => void; resumeDisabled: boolean;
}) {
  const action = progress?.next_action;
  const counts = progress?.counts;
  const safetyHold = ['unknown_attempt', 'started_action', 'pending_approval'].includes(progress?.blocker?.kind ?? '');
  const canResume = !!progress?.paused && !safetyHold && status !== 'done' && status !== 'needs_review' &&
    action?.kind !== 'review_outcome' && !!onResume;
  const current = progress?.blocker?.detail ?? (counts?.active_attempts ? 'Agents are working.' :
    counts?.queued_attempts ? 'Agent work is queued.' : status === 'done' ? 'Work completed.' :
      `No agent work is running or queued.${counts?.unfinished_assignments ? ' Some assigned results are still needed.' : ''}${counts?.ready_artifacts ? ' Saved files are ready to review.' : ''}`);
  function act() {
    if (action?.kind === 'open_agent' && action.session_id) onOpenAgent(action.session_id);
    else if (action?.kind === 'review_approval') onReviewApproval();
    else if (action?.kind === 'review_deliverables') onReviewDeliverables();
    else if (action?.kind === 'review_outcome') onReviewOutcome();
    else if (action?.kind === 'message_chief') onMessageChief();
  }
  return <section className="chief-progress" aria-label="Current work">
    <div className="chief-progress-heading"><strong>Current work</strong>{progress?.paused && <span>Paused</span>}</div>
    {error ? <p>Current work readback unavailable: {error}</p> : progress ? <>
      <p>{current}</p>
      <div className="chief-progress-counts" aria-label="Saved work counts">
        <span>{counts!.active_attempts} working</span><span>{counts!.queued_attempts} queued</span>
        <span>{counts!.unfinished_assignments} results needed</span><span>{counts!.blocked_assignments} blocked</span>
        <span>{counts!.ready_artifacts} saved files</span>
      </div>
      {(canResume || (action && action.kind !== 'none' && action.kind !== 'wait')) &&
        <div className="chief-progress-actions" role="group" aria-label="Current work actions">
          {canResume && <button className="chief-progress-resume" type="button" disabled={resumeDisabled} onClick={onResume}>Resume work</button>}
          {action && action.kind !== 'none' && action.kind !== 'wait' &&
            <button className="chief-progress-review" type="button" onClick={act}>{action.label}</button>}
        </div>}
      {action?.kind === 'wait' && <small>{action.label}</small>}
    </> : <p role="status">Reading current work…</p>}
  </section>;
}
