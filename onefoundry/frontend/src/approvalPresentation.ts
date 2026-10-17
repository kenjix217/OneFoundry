import type { Approval } from './workApi';

const internalField = /(?:^id$|_ids?$|sha(?:256)?|hash|digest|revision|commit|idempotency|actor)/i;
const identifier = /\b(?:[a-f\d]{32,64}|[a-f\d]{8}(?:-[a-f\d]{4}){3}-[a-f\d]{12})\b/gi;

/** Format system-generated proposals only. Exact approval bytes stay with the API owner. */
export function approvalPresentation(item: Approval): { target: string; content: string } {
  const structured = item.synthetic_merge || item.github_stage || item.github_fetch || item.github_snapshot || item.github_comment;
  if (!structured) return { target: item.target, content: item.content };
  const target = item.target.replace(identifier, 'saved version');
  try {
    const value: unknown = JSON.parse(item.content);
    if (!value || typeof value !== 'object' || Array.isArray(value)) return { target, content: item.content.replace(identifier, 'saved reference') };
    const fields = value as Record<string, unknown>;
    // Comment text is the actual outbound payload and must be shown unchanged.
    if (item.github_comment && typeof fields.comment === 'string') return { target, content: fields.comment };
    const describe = (record: Record<string, unknown>): string[] => Object.entries(record).flatMap(([key, field]) => {
      if (internalField.test(key) || field === null) return [];
      if (typeof field === 'object' && !Array.isArray(field)) return describe(field as Record<string, unknown>);
      if (typeof field !== 'string' && typeof field !== 'number' && typeof field !== 'boolean') return [];
      const label = key.replaceAll('_', ' ');
      return [`${label.charAt(0).toUpperCase() + label.slice(1)}: ${String(field).replace(identifier, 'saved reference')}`];
    });
    return { target, content: [...describe(fields), 'Bound to the saved version of this proposal.'].join('\n') };
  } catch {
    return { target, content: item.content.replace(identifier, 'saved reference') };
  }
}
