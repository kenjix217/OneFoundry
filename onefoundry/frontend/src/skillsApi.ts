import { ApiError, request } from './api';

export interface Skill { id: string; project_id: string; name: string; description: string; latest_version: number | null; }
export interface SkillVersion { id: string; skill_id: string; version: number; content: string; sha256: string; created_at: string; }
export interface ProfileSkill extends SkillVersion { skill_name: string; }

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ApiError('The server returned an unexpected skill response.', null);
  return value as Record<string, unknown>;
}

function field(value: unknown): string {
  if (typeof value !== 'string') throw new ApiError('The server returned an unexpected skill response.', null);
  return value;
}

function number(value: unknown): number {
  if (!Number.isSafeInteger(value) || (value as number) < 1) throw new ApiError('The server returned an unexpected skill version.', null);
  return value as number;
}

function list(value: unknown, key: string): unknown[] {
  if (Array.isArray(value)) return value;
  const envelope = object(value);
  if (Array.isArray(envelope[key])) return envelope[key];
  throw new ApiError('The server returned an unexpected skill list.', null);
}

function skill(value: unknown): Skill {
  const item = object(value);
  return { id: field(item.id), project_id: field(item.project_id), name: field(item.name),
    description: field(item.description), latest_version: item.latest_version === null ? null : number(item.latest_version) };
}

function version(value: unknown): SkillVersion {
  const item = object(value);
  return { id: field(item.id), skill_id: field(item.skill_id), version: number(item.version),
    content: field(item.content), sha256: field(item.sha256), created_at: field(item.created_at) };
}

function binding(value: unknown): ProfileSkill {
  const item = object(value);
  return { ...version(value), skill_name: field(item.skill_name) };
}

const base = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/skills`;
const profileBase = (projectId: string, profileId: string) =>
  `/api/projects/${encodeURIComponent(projectId)}/profiles/${encodeURIComponent(profileId)}/skills`;

export async function listSkills(projectId: string, signal?: AbortSignal): Promise<Skill[]> {
  return list(await request(base(projectId), { signal }), 'skills').map(skill);
}

export async function createSkill(projectId: string, name: string, description: string): Promise<Skill> {
  return skill(await request(base(projectId), { method: 'POST', body: JSON.stringify({ name, description }) }));
}

export async function listSkillVersions(projectId: string, skillId: string, signal?: AbortSignal): Promise<SkillVersion[]> {
  return list(await request(`${base(projectId)}/${encodeURIComponent(skillId)}/versions`, { signal }), 'versions').map(version);
}

export async function createSkillVersion(projectId: string, skillId: string, content: string): Promise<SkillVersion> {
  return version(await request(`${base(projectId)}/${encodeURIComponent(skillId)}/versions`, {
    method: 'POST', body: JSON.stringify({ content }),
  }));
}

export async function listProfileSkills(projectId: string, profileId: string, signal?: AbortSignal): Promise<ProfileSkill[]> {
  return list(await request(profileBase(projectId, profileId), { signal }), 'skills').map(binding);
}

export async function bindProfileSkill(projectId: string, profileId: string, skillId: string, versionNumber: number, expectedBindingId: string | null): Promise<void> {
  await request(`${profileBase(projectId, profileId)}/${encodeURIComponent(skillId)}`, {
    method: 'PUT', body: JSON.stringify({ version: versionNumber, expected_binding_id: expectedBindingId }),
  });
}

export async function removeProfileSkill(projectId: string, profileId: string, skillId: string, expectedBindingId: string): Promise<void> {
  const path = `${profileBase(projectId, profileId)}/${encodeURIComponent(skillId)}?expected_binding_id=${encodeURIComponent(expectedBindingId)}`;
  let response: Response;
  try { response = await fetch(path, { method: 'DELETE', credentials: 'same-origin' }); }
  catch { throw new ApiError('Could not reach OneFoundry. Check the saved state before retrying.', null); }
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    const detail = body && typeof body === 'object' && 'detail' in body ? (body as { detail: unknown }).detail : null;
    throw new ApiError(typeof detail === 'string' && detail ? detail : `Request failed (${response.status}). Check the saved state before retrying.`, response.status);
  }
}
