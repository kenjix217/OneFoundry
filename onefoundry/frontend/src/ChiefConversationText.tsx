import { messagePresentation } from './taskSessionActivity';

export function publicMessage(body: string): string {
  try {
    const value: unknown = JSON.parse(body);
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      const row = value as Record<string, unknown>;
      if (typeof row.feedback === 'string') return `${row.verdict === 'needs_rework' ? 'Changes requested' : 'Review'}\n${row.feedback}`;
      if (typeof row.summary === 'string') return row.summary;
      if (typeof row.reply === 'string') return row.reply;
    }
  } catch { /* Ordinary conversation text stays intact. */ }
  return body;
}
export function isToolReceiptMessage(body: string): boolean {
  try {
    const value: unknown = JSON.parse(body);
    return !!value && typeof value === 'object' && !Array.isArray(value) &&
      typeof (value as Record<string, unknown>).tool_id === 'string' &&
      typeof (value as Record<string, unknown>).status === 'string';
  } catch { return false; }
}
export function isPublicAgentMessage(body: string): boolean {
  return !isToolReceiptMessage(body) &&
    (messagePresentation(body).kind !== 'structured' || publicMessage(body) !== body);
}
export function ChiefConversationText({ body }: { body: string }) {
  return <div className="chief-message-body">{publicMessage(body)}</div>;
}
