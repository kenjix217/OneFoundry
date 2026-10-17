export type MemoryScope = 'company' | 'project';
export type AutonomyLevel = 'manual' | 'supervised' | 'bounded';
export type ProfileKind = 'builtin' | 'custom';

export interface Project {
  id: string;
  name: string;
  goal: string;
  desired_outcome: string;
  status: 'active' | 'archived' | 'trashed';
  version: number;
  trash_from_status: 'active' | 'archived' | null;
}

export interface Profile {
  id: string;
  project_id: string;
  kind: ProfileKind;
  name: string;
  reports_to_profile_id: string | null;
  model: string | null;
  model_account_id?: string | null;
  reasoning_effort?: string | null;
  instructions: string;
  memory_scope: MemoryScope;
  allowed_tools: string[];
  autonomy_level: AutonomyLevel;
  configured: boolean;
  permissions_active: boolean;
}

export interface ProfileFields {
  reports_to_profile_id?: string | null;
  model: string | null;
  model_account_id?: string | null;
  reasoning_effort?: string | null;
  instructions: string;
  memory_scope: MemoryScope;
  allowed_tools: string[];
  autonomy_level: AutonomyLevel;
}

export interface NewProfile extends ProfileFields {
  name: string;
}

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number | null,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

function record(value: unknown): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new ApiError('The server returned an unexpected response.', null);
  }
  return value as Record<string, unknown>;
}

function string(value: unknown): string {
  if (typeof value !== 'string') throw new ApiError('The server returned an unexpected response.', null);
  return value;
}

function apiErrorMessage(body: unknown, status: number): string {
  if (typeof body === 'object' && body !== null && 'detail' in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === 'string' && detail.trim()) return detail;
    if (Array.isArray(detail)) {
      const messages = detail
        .map((item) => (typeof item === 'object' && item !== null && 'msg' in item ? item.msg : null))
        .filter((message): message is string => typeof message === 'string' && message.trim().length > 0);
      if (messages.length) return messages.join(' ');
    }
  }
  return `Request failed (${status}). Check the saved state before retrying.`;
}

export async function request(path: string, init: RequestInit = {}): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      headers: { ...(init.body === undefined ? {} : { 'Content-Type': 'application/json' }), ...init.headers },
      credentials: 'same-origin',
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ApiError('Could not reach OneFoundry. Check the local service and the saved state before retrying.', null);
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new ApiError(
      response.ok ? 'The server returned an unexpected response.' : `Request failed (${response.status}).`,
      response.status,
    );
  }
  if (!response.ok) throw new ApiError(apiErrorMessage(body, response.status), response.status);
  return body;
}

function parseProject(value: unknown): Project {
  const item = record(value);
  const status = item.status ?? 'active';
  if (status !== 'active' && status !== 'archived' && status !== 'trashed') throw new ApiError('Unknown project status.', null);
  const version = item.version ?? 1;
  if (!Number.isInteger(version) || (version as number) < 1) throw new ApiError('Invalid project version.', null);
  const source = item.trash_from_status ?? null;
  if (source !== null && source !== 'active' && source !== 'archived') throw new ApiError('Invalid project Trash state.', null);
  return { id: string(item.id), name: string(item.name), goal: string(item.goal), desired_outcome: string(item.desired_outcome), status, version: version as number, trash_from_status: source };
}

function parseProfile(value: unknown): Profile {
  const item = record(value);
  if (item.kind !== 'builtin' && item.kind !== 'custom') throw new ApiError('Unknown profile kind.', null);
  if (item.memory_scope !== 'company' && item.memory_scope !== 'project') {
    throw new ApiError('Unknown memory scope.', null);
  }
  if (item.autonomy_level !== 'manual' && item.autonomy_level !== 'supervised' && item.autonomy_level !== 'bounded') {
    throw new ApiError('Unknown autonomy level.', null);
  }
  if (!Array.isArray(item.allowed_tools) || !item.allowed_tools.every((tool) => typeof tool === 'string')) {
    throw new ApiError('Invalid profile tool list.', null);
  }
  if (item.model !== null && typeof item.model !== 'string') {
    throw new ApiError('Invalid profile model.', null);
  }
  if (item.model_account_id !== null && item.model_account_id !== undefined && typeof item.model_account_id !== 'string') {
    throw new ApiError('Invalid profile model account.', null);
  }
  if (item.reasoning_effort !== null && item.reasoning_effort !== undefined &&
      (typeof item.reasoning_effort !== 'string' || !/^[a-z][a-z0-9_]{0,31}$/.test(item.reasoning_effort))) {
    throw new ApiError('Invalid profile reasoning effort.', null);
  }
  if (item.reports_to_profile_id !== null && (typeof item.reports_to_profile_id !== 'string' || !item.reports_to_profile_id.trim())) {
    throw new ApiError('Invalid reporting line.', null);
  }
  if (typeof item.configured !== 'boolean') throw new ApiError('Invalid profile configuration state.', null);
  if (item.permissions_active !== false) throw new ApiError('Invalid tool permission state.', null);
  return {
    id: string(item.id), project_id: string(item.project_id), kind: item.kind,
    name: string(item.name), reports_to_profile_id: item.reports_to_profile_id,
    model: item.model, model_account_id: item.model_account_id ?? null,
    reasoning_effort: item.reasoning_effort ?? null,
    instructions: string(item.instructions),
    memory_scope: item.memory_scope, allowed_tools: item.allowed_tools,
    autonomy_level: item.autonomy_level, configured: item.configured,
    permissions_active: item.permissions_active,
  };
}

function items(body: unknown, key: string): unknown[] {
  if (Array.isArray(body)) return body;
  const envelope = record(body);
  if (Array.isArray(envelope[key])) return envelope[key];
  throw new ApiError('The server returned an unexpected list.', null);
}

export async function listProjects(signal?: AbortSignal, status: 'active' | 'archived' | 'trashed' = 'active'): Promise<Project[]> {
  const path = status === 'active' ? '/api/projects' : `/api/projects?status=${status}`;
  return items(await request(path, { signal }), 'projects').map(parseProject);
}

export async function changeProjectStatus(project: Project, action: 'archive' | 'unarchive' | 'trash' | 'restore', confirmName?: string): Promise<Project> {
  return parseProject(await request(`/api/projects/${encodeURIComponent(project.id)}/${action}`, {
    method: 'POST', body: JSON.stringify({ expected_version: project.version, ...(action === 'trash' ? { confirm_name: confirmName } : {}) }),
  }));
}

export async function getProject(id: string, signal?: AbortSignal): Promise<Project> {
  return parseProject(await request(`/api/projects/${encodeURIComponent(id)}`, { signal }));
}

export async function createProject(name: string): Promise<Project> {
  return parseProject(await request('/api/projects', { method: 'POST', body: JSON.stringify({ name }) }));
}

export async function updateProjectBrief(projectId: string, fields: { goal: string; desired_outcome: string; expected_version: number }): Promise<Project> {
  return parseProject(await request(`/api/projects/${encodeURIComponent(projectId)}/brief`, {
    method: 'PATCH', body: JSON.stringify(fields),
  }));
}

export async function fillStarterProfiles(projectId: string): Promise<Profile[]> {
  return items(await request(`/api/projects/${encodeURIComponent(projectId)}/profiles/starter`, { method: 'POST' }), 'profiles').map(parseProfile);
}

export async function listProfiles(projectId: string, signal?: AbortSignal): Promise<Profile[]> {
  return items(await request(`/api/projects/${encodeURIComponent(projectId)}/profiles`, { signal }), 'profiles')
    .map(parseProfile);
}

export async function createProfile(projectId: string, profile: NewProfile): Promise<Profile> {
  return parseProfile(await request(`/api/projects/${encodeURIComponent(projectId)}/profiles`, {
    method: 'POST', body: JSON.stringify(profile),
  }));
}

export async function updateProfile(projectId: string, profileId: string, fields: Partial<ProfileFields>): Promise<Profile> {
  return parseProfile(await request(
    `/api/projects/${encodeURIComponent(projectId)}/profiles/${encodeURIComponent(profileId)}`,
    { method: 'PATCH', body: JSON.stringify(fields) },
  ));
}
