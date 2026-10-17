import { ApiError, request } from './api';
import { capabilityReport, type CapabilityReport } from './capabilityReadiness';

export type TeamReadiness = {
  work_order_id: string; status: string; execution_mode: string; worker_enabled: boolean;
  provider: string | null; ready_to_activate: boolean; blockers: string[];
  pending_approvals: number; queued_attempts: number; running_attempts: number;
  confirmed_results: number; next_step: string;
  prompt_context_bytes_limit?: number | null;
  ready_to_start?: boolean;
  capability_notes?: string[];
  capability_readiness?: CapabilityReport | null;
};

const base = (project: string, order: string) =>
  `/api/projects/${encodeURIComponent(project)}/work-orders/${encodeURIComponent(order)}/team-session`;

export async function getTeamReadiness(project: string, order: string, signal?: AbortSignal): Promise<TeamReadiness> {
  const value = await request(`${base(project, order)}/readiness`, { signal });
  if (!value || typeof value !== 'object' || !('blockers' in value) || !('work_order_id' in value) ||
      !('next_step' in value) || !('worker_enabled' in value) || !Array.isArray(value.blockers) ||
      value.work_order_id !== order || typeof value.next_step !== 'string' || typeof value.worker_enabled !== 'boolean') {
    throw new ApiError('Workflow readiness could not be confirmed.', null);
  }
  if ('prompt_context_bytes_limit' in value && value.prompt_context_bytes_limit !== null &&
      (typeof value.prompt_context_bytes_limit !== 'number' || !Number.isSafeInteger(value.prompt_context_bytes_limit) ||
       value.prompt_context_bytes_limit < 1024)) {
    throw new ApiError('The saved prompt limit was invalid.', null);
  }
  if (('ready_to_start' in value && typeof value.ready_to_start !== 'boolean') ||
      ('capability_notes' in value && (!Array.isArray(value.capability_notes) ||
       value.capability_notes.some((note: unknown) => typeof note !== 'string')))) {
    throw new ApiError('Workflow start readiness was invalid.', null);
  }
  if ('capability_readiness' in value && value.capability_readiness !== null) {
    capabilityReport(value.capability_readiness, project);
  }
  return value as TeamReadiness;
}

export async function activateTeam(project: string, order: string, version: number): Promise<void> {
  await request(`${base(project, order)}/activate`, {
    method: 'POST', body: JSON.stringify({ expected_version: version }),
  });
}

export async function cancelQueuedTeam(project: string, order: string, version: number): Promise<void> {
  await request(`${base(project, order)}/cancel`, {
    method: 'POST', body: JSON.stringify({ expected_version: version, client_action_id: crypto.randomUUID() }),
  });
}

export type TeamStartRequest = { client_message_id: string; expected_version: number; body: string };
export type TeamStartReceipt = { session_id: string; message_id: string; attempt_id: string;
  sequence: number; state: 'queued' };

function startReceipt(value: unknown): TeamStartReceipt {
  if (!value || typeof value !== 'object') throw new ApiError('The saved start response was incomplete.', null);
  const row = value as Record<string, unknown>;
  if (typeof row.session_id !== 'string' || typeof row.message_id !== 'string' || typeof row.attempt_id !== 'string' ||
      row.state !== 'queued' || !Number.isSafeInteger(row.sequence) || (row.sequence as number) < 1) {
    throw new ApiError('The saved start response was invalid. Check saved state before retrying.', null);
  }
  return value as TeamStartReceipt;
}

export async function startTeamWork(project: string, order: string, input: TeamStartRequest): Promise<TeamStartReceipt> {
  return startReceipt(await request(`${base(project, order)}/start`, { method: 'POST', body: JSON.stringify(input) }));
}

export async function getTeamStart(project: string, order: string, clientId: string): Promise<TeamStartReceipt> {
  return startReceipt(await request(`${base(project, order)}/start/${encodeURIComponent(clientId)}`));
}
