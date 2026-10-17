import type { ConversationTurn } from './conversationApi';
import { rejectedAction } from './taskConversationPresentation';

function sourceLabel(source: ConversationTurn['source_kind']): string {
  return source === 'system_followup' ? 'Project follow-up' : source === 'system_triage' ? 'Project triage' :
    source === 'operator_ui' ? 'Operator chat' : source === 'discord' ? 'Discord' : 'WeChat';
}

export function TaskTurnWorkLog({ turn }: { turn: ConversationTurn }) {
  const action = turn.automation_decision;
  return <details className="task-chat-turn-details task-turn-work-log">
    <summary>Work log · {rejectedAction(turn) ? 'Action rejected' : turn.status === 'done' ? 'Saved reply' : turn.status === 'running' ? 'Working' :
      turn.status === 'failed' ? 'Failed' : 'Outcome unknown'}</summary>
    <ol>
      <li><strong>Request saved</strong> · {sourceLabel(turn.source_kind)} · <time dateTime={turn.created_at}>{new Date(turn.created_at).toLocaleString()}</time></li>
      {turn.selected_skills.length > 0 && <li><strong>Procedures selected</strong> · {turn.selected_skills.map((skill) =>
        skill.skill_name ?? 'Saved procedure').join(', ')}</li>}
      {turn.used_sections.length > 0 && <li><strong>File inputs used</strong> · {turn.used_sections.map((section) => section.source_label).join(', ')}
        {turn.input_sections_omitted > 0 && ` · ${turn.input_sections_omitted} sections omitted`}</li>}
      {turn.progress_updates.map((update, index) => <li key={index}><strong>Agent update</strong> · {update}</li>)}
      {action?.status === 'saved' && <li><strong>Project change saved</strong> · {action.assignments.length > 0 ?
        `${action.assignments.length} agent assignment${action.assignments.length === 1 ? '' : 's'}` :
        action.decision.replaceAll('_', ' ')}</li>}
      {rejectedAction(turn) && <li><strong>Project change rejected</strong> · No change was saved.</li>}
      {action?.status === 'unavailable' && !rejectedAction(turn) && <li><strong>Project action unavailable</strong> · No change was saved.</li>}
      {turn.assistant_text && <li><strong>Agent reply saved</strong>{turn.finished_at && <> · <time dateTime={turn.finished_at}>
        {new Date(turn.finished_at).toLocaleString()}</time></>}</li>}
      {!turn.assistant_text && <li><strong>Reply</strong> · {turn.status === 'running' ? 'Pending' : 'No saved agent reply'}</li>}
    </ol>
    <p>Agent updates are provider-authored public summaries when available. Private reasoning is never shown. Verified run actions are shown in Progress.</p>
  </details>;
}
