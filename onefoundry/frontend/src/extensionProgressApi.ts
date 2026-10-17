import { ApiError, request } from './api';

export const extensionStages = [
  'needs_setup', 'waiting_coding', 'coding', 'coding_failed', 'coding_abandoned', 'waiting_qa', 'qa', 'qa_failed',
  'waiting_activation', 'approval_pending', 'activation_rejected', 'activating',
  'activation_failed', 'waiting_resume', 'resumed', 'unknown',
] as const;
export type ExtensionStage = typeof extensionStages[number];
export interface ExtensionAbandonment {
  attempt_id: string; run_id: string; request_sha256: string; receipt_sha256: string;
}
export interface ExtensionProgress {
  extensionKey: string;
  stage: ExtensionStage;
  title: string;
  message: string;
  abandonment?: ExtensionAbandonment;
}
function invalid(): never { throw new ApiError('The server returned invalid extension progress.', null); }
export function parseExtensionProgress(value: unknown, orderId: string): ExtensionProgress[] {
  if (!Array.isArray(value) || value.length > 50) invalid();
  const parsed = value.map((item: unknown) => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) invalid();
    const row = item as Record<string, unknown>;
    const payload = row.payload as Record<string, unknown> | undefined;
    if (!payload || typeof payload !== 'object' || Array.isArray(payload) ||
      payload.original_work_order_id !== orderId ||
      typeof row.extension_key !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(row.extension_key) ||
      !extensionStages.includes(row.stage as ExtensionStage) ||
      typeof row.title !== 'string' || !row.title || row.title.length > 100 ||
      typeof row.message !== 'string' || !row.message || row.message.length > 500) invalid();
    let abandonment: ExtensionAbandonment | undefined;
    if (row.abandonment !== undefined) {
      const value = row.abandonment as Record<string, unknown>;
      if (!value || typeof value !== 'object' || Array.isArray(value) || row.stage !== 'unknown' ||
        !['attempt_id', 'run_id'].every((key) => typeof value[key] === 'string' &&
          /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value[key] as string)) ||
        !['request_sha256', 'receipt_sha256'].every((key) => typeof value[key] === 'string' &&
          /^[0-9a-f]{64}$/.test(value[key] as string))) invalid();
      abandonment = value as unknown as ExtensionAbandonment;
    }
    return { extensionKey: row.extension_key, stage: row.stage as ExtensionStage,
      title: row.title, message: row.message, abandonment };
  });
  if (new Set(parsed.map((row) => row.extensionKey)).size !== parsed.length) invalid();
  return parsed;
}
export async function abandonExtensionBuild(
  projectId: string, candidate: ExtensionAbandonment, clientActionId: string, reason: string,
): Promise<void> {
  const result = await request(`/api/projects/${encodeURIComponent(projectId)}/runs/${candidate.run_id}/repository-code/${candidate.attempt_id}/abandon`, {
    method: 'POST', body: JSON.stringify({ client_action_id: clientActionId,
      decision: 'abandon_unverified', reason, disposable_work_discarded: true,
      request_sha256: candidate.request_sha256, receipt_sha256: candidate.receipt_sha256 }),
  });
  if (!result || typeof result !== 'object' || Array.isArray(result)) invalid();
  const saved = result as Record<string, unknown>;
  if (saved.attempt_id !== candidate.attempt_id || saved.run_id !== candidate.run_id ||
    saved.request_sha256 !== candidate.request_sha256 || saved.receipt_sha256 !== candidate.receipt_sha256 ||
    saved.client_action_id !== clientActionId || saved.status !== 'unknown' ||
    saved.decision !== 'abandon_unverified' || saved.disposable_work_discarded !== true) invalid();
}
export async function getExtensionProgress(
  projectId: string, orderId: string, signal?: AbortSignal,
): Promise<ExtensionProgress[]> {
  return parseExtensionProgress(await request(
    `/api/projects/${encodeURIComponent(projectId)}/work-orders/${encodeURIComponent(orderId)}/extensions`,
    { signal },
  ), orderId);
}
