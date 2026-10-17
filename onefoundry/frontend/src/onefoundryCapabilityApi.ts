import { ApiError, request } from './api';

export type OneFoundryRunCapability = {
  id: string;
  status: 'running' | 'done' | 'failed' | 'unknown';
  tools_active: boolean | null;
};

export type OneFoundryCapability = {
  project_id: string;
  profile_id: string;
  configured_model_id: string | null;
  configured_tool_ids: string[];
  last_run: OneFoundryRunCapability | null;
};

export async function readOneFoundryCapability(
  projectId: string,
  profileId: string,
  signal?: AbortSignal,
): Promise<OneFoundryCapability> {
  const value = await request(
    `/api/projects/${encodeURIComponent(projectId)}/profiles/${encodeURIComponent(profileId)}/onefoundry-capability`,
    { signal },
  );
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new ApiError('Invalid capability readback.', null);
  const row = value as Record<string, unknown>;
  const run = row.last_run;
  if (
    row.project_id !== projectId || row.profile_id !== profileId ||
    !(row.configured_model_id === null || typeof row.configured_model_id === 'string') ||
    !Array.isArray(row.configured_tool_ids) || !row.configured_tool_ids.every((id) => typeof id === 'string') ||
    !(run === null || (
      typeof run === 'object' && !Array.isArray(run) &&
      typeof (run as Record<string, unknown>).id === 'string' &&
      ['running', 'done', 'failed', 'unknown'].includes(String((run as Record<string, unknown>).status)) &&
      (typeof (run as Record<string, unknown>).tools_active === 'boolean' ||
        (run as Record<string, unknown>).tools_active === null)
    ))
  ) throw new ApiError('Invalid capability readback.', null);
  return value as OneFoundryCapability;
}
