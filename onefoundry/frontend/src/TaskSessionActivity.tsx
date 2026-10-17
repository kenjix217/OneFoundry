import { useState } from 'react';

import type { Profile } from './api';
import type { TeamMessage, TeamSessionView } from './taskSessionApi';
import { TaskMessageActions } from './TaskMessageActions';
import { actionSummary, activityEntries, activityRecord, messageAttempt, messagePresentation,
  recordedTiming, safeActivityText, safeDiagnostics, textField, toolIdentity, type MessagePresentation } from './taskSessionActivity';

function presentationForMessage(message: TeamMessage): MessagePresentation {
  return message.kind.endsWith('operator') ? { kind: 'text', text: safeActivityText(message.body) } :
    messagePresentation(message.body);
}

function Timestamp({ value }: { value: string }) {
  const date = new Date(value);
  return <time dateTime={Number.isNaN(date.valueOf()) ? undefined : value}>
    {Number.isNaN(date.valueOf()) ? 'Time not recorded' : date.toLocaleString()}</time>;
}

function Diagnostics({ value }: { value: unknown }) {
  const [open, setOpen] = useState(false);
  return <details className="task-activity-diagnostics" open={open}><summary onClick={(event) => {
    event.preventDefault(); setOpen((value) => !value);
  }}>Technical details</summary>{open && <pre>{safeDiagnostics(value)}</pre>}</details>;
}

function RecordedText({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return text.length > 800 ? <><p>{text.slice(0, 800)}…</p><details className="task-activity-long-text" open={open}>
    <summary onClick={(event) => { event.preventDefault(); setOpen((value) => !value); }}>Read full text ({text.length.toLocaleString()} characters)</summary>
    {open && <p>{text}</p>}</details></> : <p>{text}</p>;
}

function ArtifactAction({ result, onOpenRun }: { result: Record<string, unknown> | null; onOpenRun?: (id: string) => void }) {
  const payload = activityRecord(result?.result) ?? result;
  const artifact = activityRecord(payload?.artifact) ?? payload;
  const run = textField(artifact, 'run_id') ?? textField(result, 'run_id');
  return typeof artifact?.artifact_id === 'string' && run && onOpenRun
    ? <button type="button" className="button button-outline" onClick={() => onOpenRun(run)}>Open artifact run</button> : null;
}

export function TaskSessionActivity({ view, profiles, onOpenRun, onOpenSession, onCopyMessage, onEditMessage }: {
  view: TeamSessionView; profiles: Profile[]; onOpenRun?: (id: string) => void; onOpenSession?: (id: string) => void;
  onCopyMessage?: (text: string) => void; onEditMessage?: (id: string, text: string) => void;
}) {
  const entries = activityEntries(view);
  const [showActivity, setShowActivity] = useState(false);
  const isConversation = (entry: typeof entries[number]) => entry.kind === 'message' && entry.item.kind !== 'system' &&
    presentationForMessage(entry.item).kind === 'text';
  const activityCount = entries.filter((entry) => !isConversation(entry)).length;
  const visibleEntries = showActivity ? entries : entries.filter(isConversation);
  const turns = entries.filter((entry) => entry.kind === 'attempt');
  const number = (id: string | null) => { const index = turns.findIndex((entry) => entry.item.id === id); return index < 0 ? null : index + 1; };
  const author = (message: TeamMessage) => message.kind.endsWith('operator') ? 'You' : message.kind === 'system' ? 'System record' :
    profiles.find((profile) => profile.id === (message.actor_id.startsWith('profile:') ? message.actor_id.slice(8) : view.session.profile_id))?.name ?? 'Agent';
  const partial = !!view.history_omitted?.attempts;
  return <section className="task-session-activity" aria-label="Recorded worker activity">
    <p className="task-activity-provenance">Agent messages are emitted text. Turn and action summaries come from saved records.
      Provider loop iterations and private reasoning are not recorded.</p>
    {activityCount > 0 && <button type="button" className="task-activity-toggle" aria-expanded={showActivity}
      onClick={() => setShowActivity((value) => !value)}>{showActivity ? 'Hide' : 'Show'} agent action activity ({activityCount})</button>}
    {entries.length === 0 && <p>No agent messages or activity recorded yet.</p>}
    {visibleEntries.map((entry) => {
      if (entry.kind === 'attempt') {
        const attempt = entry.item;
        const n = number(attempt.id);
        const retry = number(attempt.retry_of_attempt_id ?? null);
        return <article className="task-activity-turn" key={entry.key}>
          <h3>Turn {n}{partial ? ' (visible history)' : ''} · {attempt.status === 'done' ? 'Turn finished' : attempt.status === 'running' ? 'Agent working' :
            attempt.status === 'queued' ? 'Agent queued' : attempt.status === 'unknown' ? 'Outcome unknown' : 'Turn stopped'}</h3>
          <p><Timestamp value={attempt.created_at} /> · {recordedTiming(attempt.started_at, attempt.finished_at)}
            {attempt.finished_at && <> · Finished <Timestamp value={attempt.finished_at} /></>}</p>
          <p>{attempt.provider ? `${safeActivityText(attempt.provider)}${attempt.model_id ? ` ${safeActivityText(attempt.model_id)}` : ''}` : 'Provider: not recorded'}
            {' · '}{attempt.input_tokens !== null && attempt.output_tokens !== null
              ? `${(attempt.input_tokens + attempt.output_tokens).toLocaleString()} reported tokens` : 'Token use: not recorded'}
            {' · '}{attempt.cost_usd === null ? 'Cost: not recorded' : `Reported cost: $${safeActivityText(attempt.cost_usd)}`}</p>
          {attempt.retry_of_attempt_id && <p>Retry of {retry ? `turn ${retry}` : 'an earlier turn outside visible history'}.
            {attempt.not_before && <> Scheduled after <Timestamp value={attempt.not_before} />.</>}</p>}
          {attempt.reason && <p className="task-activity-error">Recorded stop reason: {safeActivityText(attempt.reason).replaceAll('_', ' ')}</p>}
          {attempt.status === 'unknown' && <p>{attempt.unknown_acknowledged ? 'The operator abandoned this unverified result. Its history remains unknown.' :
            'Confirmed work is retained. Check and reconcile uncertain saved actions before deciding whether to continue.'}</p>}
          <Diagnostics value={attempt} />
        </article>;
      }
      if (entry.kind === 'tool') {
        const tool = entry.item;
        const identity = toolIdentity(tool.tool_name, tool.result);
        const n = number(tool.attempt_id);
        const actions = entries.filter((item) => item.kind === 'tool' && item.item.attempt_id === tool.attempt_id);
        const index = actions.findIndex((item) => item.key === entry.key) + 1;
        const nestedStatus = textField(tool.result, 'status');
        const dependencyState = tool.status === 'done' && tool.tool_name === 'onefoundry.read_dependency_result' &&
          typeof tool.result?.content_available === 'boolean' && ['pending', 'blocked', 'canceled'].includes(nestedStatus ?? '')
          ? nestedStatus : null;
        return <article className={`task-activity-action status-${tool.status}`} key={entry.key}>
          <small>{n ? `Turn ${n}` : 'Turn outside visible history'} · Action {index} · Recorded action</small>
          <h4>{identity.label}</h4><p className="task-activity-meta">{identity.name}
            {' · '}{dependencyState === 'pending' ? 'waiting for result' : dependencyState === 'blocked' ? 'rework required' :
              dependencyState === 'canceled' ? 'assignment canceled' : tool.status === 'done' ? 'saved' : tool.status === 'failed' ? 'failed' : 'outcome unconfirmed'}
            {nestedStatus && nestedStatus !== tool.status && <> · Operation: {nestedStatus.replaceAll('_', ' ')}</>}</p>
          <p><Timestamp value={tool.created_at} /> · {recordedTiming(tool.created_at, tool.finished_at)}</p>
          <RecordedText text={tool.reason === 'dependency_not_accepted' && tool.result === null
            ? 'This plan depends on rejected or canceled work. Use its feedback to revise the assignment, then request a new review.'
            : actionSummary(tool.result, tool.tool_name)} />
          {tool.tool_name === 'onefoundry.role_tool' && !textField(tool.result, 'tool_id') && <p>Role tool name and arguments: not recorded.</p>}
          {tool.reason && <p className="task-activity-error">Recorded action error: {safeActivityText(tool.reason).replaceAll('_', ' ')}</p>}
          <ArtifactAction result={tool.result} onOpenRun={onOpenRun} />
          {typeof tool.result?.child_session_id === 'string' && onOpenSession && view.children.some((item) => item.id === tool.result?.child_session_id) &&
            <button type="button" className="button button-outline" onClick={() => onOpenSession(tool.result!.child_session_id as string)}>Open assigned agent</button>}
          <Diagnostics value={tool} />
        </article>;
      }
      const message = entry.item;
      const presented = presentationForMessage(message);
      const n = number(messageAttempt(message, view));
      return <article key={entry.key} className={`task-chat-bubble ${message.kind.endsWith('operator') ? 'task-chat-operator' : 'task-chat-agent'} task-activity-message`}>
        <div><strong>{presented.kind === 'receipt' ? 'Recorded tool receipt' : author(message)}</strong><Timestamp value={message.created_at} /></div>
        <small>Message {message.sequence} · {n ? `Turn ${n}` : 'Turn link: not recorded'} · {presented.kind === 'text' ?
          message.kind === 'system' ? 'System text' : message.kind.endsWith('operator') ? 'Operator text' : 'Emitted agent text' : 'Recorded summary'}</small>
        {presented.receipt ? <><h4>{toolIdentity('onefoundry.role_tool', presented.receipt).label}</h4>
          <p className="task-activity-meta">Recorded operation: {textField(presented.receipt, 'status')?.replaceAll('_', ' ') ?? 'not recorded'}</p>
          <RecordedText text={actionSummary(presented.receipt)} /><ArtifactAction result={presented.receipt} onOpenRun={onOpenRun} /></> :
          <RecordedText text={presented.text} />}
        {presented.record && <ArtifactAction result={presented.record} onOpenRun={onOpenRun} />}
        {presented.kind === 'text' && message.kind !== 'system' && onCopyMessage &&
          <TaskMessageActions source={message.kind.endsWith('operator') ? 'your' : 'agent'} text={presented.text}
            onCopy={onCopyMessage} onEdit={onEditMessage ? () => onEditMessage(message.id, presented.text) : undefined} />}
        <Diagnostics value={presented.kind === 'text' ? { ...message, body: presented.text } :
          { ...message, body: (() => { try { return JSON.parse(message.body); } catch { return safeActivityText(message.body); } })() }} />
      </article>;
    })}
  </section>;
}
