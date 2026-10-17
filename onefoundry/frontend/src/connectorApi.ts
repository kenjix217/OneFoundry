import { ApiError, request } from './api';

export type ConnectorProvider = 'microsoft' | 'google';
export type ConnectorFeature = 'mail' | 'files' | 'files_write' | 'sign_out_everywhere';

export interface Connector {
  id: string;
  provider: ConnectorProvider;
  label: string;
  client_id: string;
  tenant: string;
  features: ConnectorFeature[];
  status: 'pending' | 'connected' | 'failed';
  last_error_code: string | null;
  granted_scopes: string;
  version: number;
}

export interface NewConnector {
  provider: ConnectorProvider;
  label: string;
  client_id: string;
  tenant: string;
  client_secret?: string;
  features: ConnectorFeature[];
}

export type SignIn =
  | { kind: 'device'; user_code: string; verification_uri: string; expires_at: number; interval: number }
  | { kind: 'redirect'; authorize_url: string };

function bad(): never {
  throw new ApiError('The server returned an unexpected account response.', null);
}

function connector(value: unknown): Connector {
  if (!value || typeof value !== 'object' || Array.isArray(value)) bad();
  const row = value as Record<string, unknown>;
  const text = (key: string) => { if (typeof row[key] !== 'string') bad(); return row[key] as string; };
  const provider = text('provider');
  const status = text('status');
  if ((provider !== 'microsoft' && provider !== 'google') || !['pending', 'connected', 'failed'].includes(status)) bad();
  if (!Array.isArray(row.features) || !Number.isSafeInteger(row.version)) bad();
  return { id: text('id'), provider, label: text('label'), client_id: text('client_id'), tenant: text('tenant'),
    features: row.features.map(String) as ConnectorFeature[], status: status as Connector['status'],
    last_error_code: row.last_error_code === null ? null : text('last_error_code'), granted_scopes: text('granted_scopes'),
    version: row.version as number };
}

const base = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/connectors`;

export async function listConnectors(projectId: string, signal?: AbortSignal): Promise<Connector[]> {
  const value = await request(base(projectId), { signal });
  if (!Array.isArray(value)) bad();
  return value.map(connector);
}

export async function createConnector(projectId: string, fields: NewConnector): Promise<Connector> {
  return connector(await request(base(projectId), { method: 'POST', body: JSON.stringify(fields) }));
}

export async function startSignIn(projectId: string, id: string): Promise<SignIn> {
  const value = await request(`${base(projectId)}/${encodeURIComponent(id)}/sign-in`, { method: 'POST', body: '{}' });
  if (!value || typeof value !== 'object') bad();
  const row = value as Record<string, unknown>;
  if (row.kind === 'device' && typeof row.user_code === 'string' && typeof row.verification_uri === 'string') {
    return { kind: 'device', user_code: row.user_code, verification_uri: row.verification_uri,
      expires_at: Number(row.expires_at), interval: Number(row.interval) };
  }
  if (row.kind === 'redirect' && typeof row.authorize_url === 'string' && row.authorize_url.startsWith('https://accounts.google.com/')) {
    return { kind: 'redirect', authorize_url: row.authorize_url };
  }
  bad();
}

export async function pollSignIn(projectId: string, id: string): Promise<Connector> {
  return connector(await request(`${base(projectId)}/${encodeURIComponent(id)}/poll`, { method: 'POST', body: '{}' }));
}

export async function signOutEverywhere(projectId: string, item: Connector, requestId: string): Promise<string> {
  const value = await request(`${base(projectId)}/${encodeURIComponent(item.id)}/sign-out-everywhere`, {
    method: 'POST', body: JSON.stringify({ request_id: requestId, expected_version: item.version,
      confirmation: 'sign_out_all_apps_and_browsers' }),
  });
  if (!value || typeof value !== 'object') bad();
  const state = (value as Record<string, unknown>).state;
  if (state === 'completed') return 'Microsoft accepted signing out all apps and browsers. It may take a few minutes. OneFoundry now needs a new sign-in.';
  if (state === 'failed') return 'Microsoft rejected the sign-out. OneFoundry is paused for this account; check its permissions before signing in again.';
  if (state === 'unknown' || state === 'dispatching') return 'The sign-out outcome is unconfirmed. It has not been retried. Check your Microsoft account sessions before continuing.';
  bad();
}

export async function removeConnector(projectId: string, id: string, expectedVersion: number): Promise<void> {
  const response = await fetch(`${base(projectId)}/${encodeURIComponent(id)}?expected_version=${expectedVersion}`, {
    method: 'DELETE', credentials: 'same-origin',
  });
  if (response.status !== 204) {
    let detail = `Removing the account failed (${response.status}).`;
    try { const body = await response.json() as { detail?: unknown }; if (typeof body.detail === 'string') detail = body.detail; } catch { /* keep default */ }
    throw new ApiError(detail, response.status);
  }
}

/** Finish a Google sign-in that returned to the app root with ?code and ?state. */
export async function completeGoogleRedirect(location: Location = window.location): Promise<boolean> {
  const params = new URLSearchParams(location.search);
  const code = params.get('code');
  const state = params.get('state');
  if (!code || !state) return false;
  try {
    await request('/api/connectors/google/callback', { method: 'POST', body: JSON.stringify({ code, state }) });
  } finally {
    window.history.replaceState({}, '', location.pathname);
  }
  return true;
}
