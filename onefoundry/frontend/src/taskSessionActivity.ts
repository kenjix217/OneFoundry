import type { TeamAttempt, TeamMessage, TeamSessionView, TeamToolCall } from './taskSessionApi';

type RecordValue = Record<string, unknown>;
export type ActivityEntry = { key: string; at: string } & (
  { kind: 'message'; item: TeamMessage } | { kind: 'attempt'; item: TeamAttempt } | { kind: 'tool'; item: TeamToolCall });
export type MessagePresentation = { kind: 'text' | 'receipt' | 'structured'; text: string; receipt?: RecordValue; record?: RecordValue };
const PRIVATE_FIELD = /^(?:analysis|reasoning.*|thinking|chain.?of.?thought|private.?reasoning.*)$/i;
const SECRET_FIELD = /(?:password|secret|credential|authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|^token$)/i;

export function safeActivityText(text: string): string {
  return text.replace(/-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)/g, '[redacted]')
    .replace(/\b(?:sk-[A-Za-z0-9_-]{12,}|sk_live_[A-Za-z0-9]{12,}|ghp_[A-Za-z0-9]{12,}|github_pat_[A-Za-z0-9_]{12,}|xox[baprs]-[A-Za-z0-9-]{12,}|AKIA[A-Z0-9]{16})\b/g, '[redacted]')
    .replace(/\bBearer\s+[A-Za-z0-9._~-]+/gi, 'Bearer [redacted]')
    .replace(/\b((?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret)\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;}]+)/gi, '$1[redacted]')
    .replace(/(https?:\/\/)[^\s/@]+:[^\s/@]+@/gi, '$1[redacted]@')
    .replace(/([?&](?:token|key|api_key|signature|sig|password)=)[^&#\s]+/gi, '$1[redacted]');
}

export function activityRecord(value: unknown): RecordValue | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as RecordValue : null;
}

export function safeDiagnostics(value: unknown): string {
  function clean(item: unknown, depth = 0): unknown {
    if (depth > 20) return '[nested data omitted]';
    if (typeof item === 'string') {
      if (/^\s*[[{]/.test(item)) {
        try { return JSON.stringify(clean(JSON.parse(item), depth + 1)); } catch { /* Preserve malformed data as sanitized text. */ }
      }
      return safeActivityText(item);
    }
    if (Array.isArray(item)) return item.map((child) => clean(child, depth + 1));
    const record = activityRecord(item);
    if (!record) return item;
    return Object.fromEntries(Object.entries(record).filter(([key]) => !PRIVATE_FIELD.test(key))
      .map(([key, child]) => [safeActivityText(key), SECRET_FIELD.test(key) ? '[redacted]' : clean(child, depth + 1)]));
  }
  return JSON.stringify(clean(value), null, 2);
}

export function textField(record: RecordValue | null | undefined, name: string): string | null {
  return typeof record?.[name] === 'string' && record[name].trim() ? safeActivityText(record[name]) : null;
}

export function messagePresentation(body: string): MessagePresentation {
  try {
    const value: unknown = JSON.parse(body);
    const record = activityRecord(value);
    if (record && typeof record.tool_id === 'string' && typeof record.status === 'string') {
      return { kind: 'receipt', text: 'Saved tool receipt', receipt: record };
    }
    if (record && (record.verdict === 'passed' || record.verdict === 'needs_rework') && typeof record.feedback === 'string') {
      return { kind: 'text', record, text: `${record.verdict === 'passed' ? 'QA passed' : 'QA needs rework'}\n${safeActivityText(record.feedback)}` };
    }
    if (record && typeof record.summary === 'string') {
      const content = textField(activityRecord(record.artifact), 'content');
      return { kind: 'text', record, text: safeActivityText(record.summary) + (content ? `\n\n${content}` : '') };
    }
    if (record && typeof record.reply === 'string') return { kind: 'text', text: safeActivityText(record.reply) };
    return record || Array.isArray(value)
      ? { kind: 'structured', text: 'Structured agent message. A readable summary was not recorded.' }
      : { kind: 'text', text: safeActivityText(body) };
  } catch {
    return /^\s*\{/.test(body) || /^\s*\[/.test(body) && !/^\s*\[[^\]\n]+\](?:\(|\s|$)/.test(body)
      ? { kind: 'structured', text: 'Unparsed structured message. A readable summary was not recorded.' }
      : { kind: 'text', text: safeActivityText(body) };
  }
}

const TOOL_LABELS: Record<string, string> = {
  'onefoundry.publish_update': 'Public update', 'onefoundry.delegate_agent': 'Agent assignment',
  'onefoundry.report_result': 'Result report', 'onefoundry.submit_completion': 'Submitted for operator review',
  'onefoundry.cancel_assignment': 'Assignment cancellation', 'onefoundry.role_tool': 'Role tool',
  write_text_artifact_v1: 'Save text artifact', read_text_artifact_v1: 'Read text artifact',
  request_video_capture_v1: 'Request video capture', public_web_search_v1: 'Search the web',
  public_web_fetch_v1: 'Read web source', decimal_calculate_v1: 'Calculate',
};

export function toolIdentity(toolName: string, result: RecordValue | null): { name: string; label: string } {
  const name = textField(result, 'tool_id') ?? safeActivityText(toolName);
  const label = TOOL_LABELS[name] ?? name.replace(/^onefoundry\./, '').replace(/_v\d+$/, '').replaceAll('_', ' ');
  return { name, label: label ? label[0].toUpperCase() + label.slice(1) : 'Tool name not recorded' };
}

const CHIEF_INSPECTION_TOOLS = new Set([
  'onefoundry.read_project_status', 'onefoundry.read_work_order', 'onefoundry.read_agent_session',
]);

export function actionSummary(result: RecordValue | null, toolName?: string): string {
  if (!result) return 'Result details not recorded.';
  if (toolName && CHIEF_INSPECTION_TOOLS.has(toolName)) {
    return 'Saved workflow records read. The full receipt is in Technical details.';
  }
  const payload = activityRecord(result.result) ?? result;
  const kind = textField(payload, 'kind');
  const bytes = payload.byte_size;
  if (typeof payload.artifact_id === 'string') return `Artifact receipt${kind ? ` · ${kind}` : ''}` +
    (typeof bytes === 'number' && Number.isSafeInteger(bytes) && bytes >= 0 ? ` · ${bytes.toLocaleString()} bytes` : ' · size not recorded');
  const fields = ['summary', 'feedback', 'detail', 'message', 'reason', 'next_step', 'text', 'output', 'content', 'stdout', 'stderr', 'value']
    .flatMap((key) => textField(payload, key) ? [textField(payload, key)!] : []);
  if (fields.length) return fields.join('\n');
  if (typeof payload.child_session_id === 'string') return 'Agent session and assignment saved.';
  if (typeof payload.message_id === 'string' || typeof payload.result_message_id === 'string') return 'Message saved.';
  const counts = ['sources', 'matches', 'results', 'files', 'artifacts'].flatMap((key) => Array.isArray(payload[key])
    ? [`${payload[key].length.toLocaleString()} ${key} recorded`] : []);
  if (counts.length) return counts.join(' · ');
  const status = textField(payload, 'status');
  return status ? `Recorded result: ${status.replaceAll('_', ' ')}.` : 'Receipt saved. Result summary not recorded.';
}

export function recordedTiming(start: string | null | undefined, finish: string | null): string {
  if (!start) return 'Duration: not recorded';
  const duration = finish ? new Date(finish).valueOf() - new Date(start).valueOf() : NaN;
  return Number.isFinite(duration) && duration >= 0 ? `Duration: ${(duration / 1000).toLocaleString()}s` : 'Duration: not recorded';
}

export function activityEntries(view: TeamSessionView): ActivityEntry[] {
  // IDs are the reconciliation identity. Preserve distinct repeated text and replace streamed records by ID.
  const entries: ActivityEntry[] = [
    ...view.messages.map((item) => ({ key: `message:${item.id}`, kind: 'message' as const, at: item.created_at, item })),
    ...view.attempts.map((item) => ({ key: `attempt:${item.id}`, kind: 'attempt' as const, at: item.created_at, item })),
    ...view.tool_calls.map((item) => ({ key: `tool:${item.id}`, kind: 'tool' as const, at: item.created_at, item })),
  ];
  return [...new Map(entries.map((entry) => [entry.key, entry])).values()].sort((left, right) => {
    const time = new Date(left.at).valueOf() - new Date(right.at).valueOf();
    if (Number.isFinite(time) && time !== 0) return time;
    if (left.kind === 'message' && right.kind === 'message') return left.item.sequence - right.item.sequence || left.key.localeCompare(right.key);
    const rank = { attempt: 0, message: 1, tool: 2 };
    return rank[left.kind] - rank[right.kind] || left.key.localeCompare(right.key);
  });
}

export function messageAttempt(message: TeamMessage, view: TeamSessionView): string | null {
  const receipt = messagePresentation(message.body).receipt;
  if (typeof receipt?.run_id === 'string' && view.attempts.some((item) => item.id === receipt.run_id)) return receipt.run_id;
  const tool = view.tool_calls.find((item) => item.result?.message_id === message.id || item.result?.result_message_id === message.id);
  return tool?.attempt_id ?? view.attempts.find((item) => item.source_message_id === message.id)?.id ?? null;
}
