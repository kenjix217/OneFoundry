import { ApiError, request } from './api';

export type ChiefConversation = {
  id: string; title: string; status: string; version: number; profile_id: string; profile_name: string;
  archived: boolean; paused: boolean; controls_version: number; canonical_chief: boolean;
  trashed?: boolean;
  direct_agent?: boolean;
  completion_policy: 'chief_verified' | 'operator_review'; state: string; detail?: string | null;
};
export type ChiefMessageRequest = { client_message_id: string; body: string; order_id?: string;
  expected_version?: number; expected_sequence?: number; profile_id?: string };
export type ChiefReceipt = { order_id: string; session_id: string; message_id: string; attempt_id: string | null;
  sequence: number; disposition: 'queued' | 'coalesced' | 'discussion' | 'awaiting_setup'; detail: string | null };

function base(projectId: string) { return `/api/projects/${encodeURIComponent(projectId)}/chief-workspace`; }
function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ApiError('Chief readback is incomplete.', null);
  return value as Record<string, unknown>;
}
function receipt(value: unknown): ChiefReceipt {
  const row = record(value);
  if (!['order_id', 'session_id', 'message_id'].every((key) => typeof row[key] === 'string') ||
      !(row.attempt_id === null || typeof row.attempt_id === 'string') || !Number.isSafeInteger(row.sequence) ||
      !['queued', 'coalesced', 'discussion', 'awaiting_setup'].includes(String(row.disposition)) ||
      !(row.detail === null || typeof row.detail === 'string')) throw new ApiError('Chief message could not be confirmed.', null);
  return row as unknown as ChiefReceipt;
}
export async function listChiefConversations(projectId: string, signal?: AbortSignal): Promise<ChiefConversation[]> {
  const rows = await request(base(projectId), { signal });
  if (!Array.isArray(rows)) throw new ApiError('Conversations could not be read.', null);
  return rows.map((value: unknown) => {
    const row = record(value);
    if (!['id', 'title', 'status', 'profile_id', 'profile_name', 'state'].every((key) => typeof row[key] === 'string') ||
        !['archived', 'paused', 'canonical_chief'].every((key) => typeof row[key] === 'boolean') ||
        !(row.trashed === undefined || typeof row.trashed === 'boolean') ||
        !(row.direct_agent === undefined || typeof row.direct_agent === 'boolean') ||
        !Number.isSafeInteger(row.version) || !Number.isSafeInteger(row.controls_version) ||
        !['chief_verified', 'operator_review'].includes(String(row.completion_policy)) ||
        !(row.detail === undefined || row.detail === null || typeof row.detail === 'string')) throw new ApiError('Conversation state is incomplete.', null);
    return row as unknown as ChiefConversation;
  });
}
export async function sendChiefMessage(projectId: string, data: ChiefMessageRequest): Promise<ChiefReceipt> {
  return receipt(await request(`${base(projectId)}/messages`, { method: 'POST', body: JSON.stringify(data) }));
}
export async function lookupChiefMessage(projectId: string, id: string): Promise<ChiefReceipt> {
  return receipt(await request(`${base(projectId)}/messages/${encodeURIComponent(id)}`));
}
export async function resolveChiefConversation(projectId: string, orderId: string, signal?: AbortSignal): Promise<{ order_id: string; agent_session_id: string | null }> {
  const row = record(await request(`${base(projectId)}/resolve/${encodeURIComponent(orderId)}`, { signal }));
  if (typeof row.order_id !== 'string' || !(row.agent_session_id === null || typeof row.agent_session_id === 'string')) throw new ApiError('Conversation link could not be resolved.', null);
  return row as { order_id: string; agent_session_id: string | null };
}
export async function changeChiefConversation(projectId: string, order: ChiefConversation,
  action: 'rename' | 'pause' | 'resume' | 'archive' | 'restore' | 'trash', title?: string): Promise<void> {
  const result = record(await request(`${base(projectId)}/${encodeURIComponent(order.id)}/controls`, {
    method: 'POST', body: JSON.stringify({ client_action_id: crypto.randomUUID(), expected_version: order.version,
      expected_controls_version: order.controls_version, action, ...(title === undefined ? {} : { title }) }),
  }));
  if (result.order_id !== order.id || result.action !== action || result.controls_version !== order.controls_version + 1)
    throw new ApiError('The conversation action could not be confirmed. Refresh before retrying.', null);
}
