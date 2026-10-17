import type { ConversationTurn } from './conversationApi';

export function rejectedAction(turn: ConversationTurn): boolean {
  return turn.automation_decision?.status === 'rejected' ||
    turn.automation_decision?.error_code === 'invalid_action_proposal' ||
    turn.automation_decision?.error_code === 'invalid_decision_format' && !!turn.assistant_text?.trimStart().startsWith('{');
}

export function visibleReply(turn: ConversationTurn): string | null {
  if (rejectedAction(turn)) {
    return 'The agent’s proposed action was rejected. No team assignment, rule, or project change was saved by this reply.';
  }
  const raw = turn.assistant_text;
  if (!raw || turn.automation_decision?.error_code !== 'invalid_decision_format') return raw;
  try {
    const proposal: unknown = JSON.parse(raw);
    if (typeof proposal === 'object' && proposal !== null && !Array.isArray(proposal) &&
        typeof (proposal as { reply?: unknown }).reply === 'string') {
      return (proposal as { reply: string }).reply.trim() || raw;
    }
  } catch { /* Keep historical text if it was not JSON. */ }
  return raw;
}
