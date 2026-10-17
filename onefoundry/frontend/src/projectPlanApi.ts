import { ApiError, request } from './api';

export interface ProjectPlan {
  project_id: string;
  body: string;
  version: number;
  actor_id: string;
  updated_at: string;
}

function parsePlan(value: unknown, projectId: string): ProjectPlan {
  if (typeof value !== 'object' || value === null || Array.isArray(value))
    throw new ApiError('The server returned an invalid project plan.', null);
  const row = value as Record<string, unknown>;
  if (row.project_id !== projectId || typeof row.body !== 'string' ||
      !Number.isSafeInteger(row.version) || (row.version as number) < 1 ||
      typeof row.actor_id !== 'string' || typeof row.updated_at !== 'string')
    throw new ApiError('The server returned an invalid project plan.', null);
  return row as unknown as ProjectPlan;
}

function path(projectId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/plan`;
}

export async function getProjectPlan(projectId: string, signal?: AbortSignal): Promise<ProjectPlan> {
  return parsePlan(await request(path(projectId), { signal }), projectId);
}

export async function updateProjectPlan(
  projectId: string, body: string, expectedVersion: number,
): Promise<ProjectPlan> {
  return parsePlan(await request(path(projectId), {
    method: 'PATCH', body: JSON.stringify({ body, expected_version: expectedVersion }),
  }), projectId);
}
