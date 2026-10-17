import { ApiError, request } from './api';

export interface ZohoConnection {
  id: string;
  label: string;
  status: 'needs_credentials' | 'unverified' | 'ready' | 'failed';
  enabled: boolean;
  credential_present: boolean;
  endpoint_present: boolean;
  oauth_pending: boolean;
  mail_account_id: string | null;
  from_address: string | null;
  last_error_code: string | null;
  last_probe_at: string | null;
  version: number;
}

function connection(value: unknown): ZohoConnection {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ApiError('Unexpected Zoho connection response.', null);
  const row = value as Record<string, unknown>;
  if (typeof row.id !== 'string' || typeof row.label !== 'string' ||
      !['needs_credentials', 'unverified', 'ready', 'failed'].includes(String(row.status)) ||
      typeof row.enabled !== 'boolean' || typeof row.credential_present !== 'boolean' ||
      !Number.isSafeInteger(row.version) ||
      !['mail_account_id', 'from_address', 'last_error_code', 'last_probe_at'].every((key) => row[key] === null || typeof row[key] === 'string')) {
    throw new ApiError('Unexpected Zoho connection response.', null);
  }
  if (row.endpoint_present !== undefined && typeof row.endpoint_present !== 'boolean') throw new ApiError('Unexpected Zoho endpoint status.', null);
  if (row.oauth_pending !== undefined && typeof row.oauth_pending !== 'boolean') throw new ApiError('Unexpected Zoho sign-in status.', null);
  return { ...row, endpoint_present: row.endpoint_present ?? row.credential_present, oauth_pending: row.oauth_pending ?? false } as unknown as ZohoConnection;
}

const root = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/mail/zoho`;
export async function listZohoConnections(projectId: string, signal?: AbortSignal): Promise<ZohoConnection[]> {
  const rows = await request(`${root(projectId)}/connections`, { signal });
  if (!Array.isArray(rows)) throw new ApiError('Unexpected Zoho connection list.', null);
  return rows.map(connection);
}
export async function saveZohoSetup(projectId: string, label: string): Promise<ZohoConnection> {
  return connection(await request(`${root(projectId)}/connections`, { method: 'POST', body: JSON.stringify({ label }) }));
}
export async function saveZohoCredentials(projectId: string, row: ZohoConnection, serverUrl: string, token: string, accountId: string): Promise<ZohoConnection> {
  return connection(await request(`${root(projectId)}/connections/${encodeURIComponent(row.id)}/credentials`, {
    method: 'POST', body: JSON.stringify({ expected_version: row.version, server_url: serverUrl, access_token: token, mail_account_id: accountId }),
  }));
}
export async function saveZohoEndpoint(projectId: string, row: ZohoConnection, serverUrl: string, accountId: string): Promise<ZohoConnection> {
  return connection(await request(`${root(projectId)}/connections/${encodeURIComponent(row.id)}/endpoint`, {
    method: 'POST', body: JSON.stringify({ expected_version: row.version, server_url: serverUrl, mail_account_id: accountId }),
  }));
}
export async function startZohoSignIn(projectId: string, row: ZohoConnection): Promise<string> {
  const base = `${root(projectId)}/connections/${encodeURIComponent(row.id)}`;
  const result = await request(`${base}/sign-in`, { method: 'POST', body: JSON.stringify({ expected_version: row.version }) });
  if (!result || typeof result !== 'object' || (result as Record<string, unknown>).authorize_path !== `${base}/oauth/authorize`) {
    throw new ApiError('Could not confirm the Zoho sign-in page. Refresh and connect again.', null);
  }
  return `${base}/oauth/authorize`;
}
export async function probeZoho(projectId: string, row: ZohoConnection): Promise<ZohoConnection> {
  return connection(await request(`${root(projectId)}/connections/${encodeURIComponent(row.id)}/probe`, { method: 'POST', body: '{}' }));
}
export async function enableZoho(projectId: string, row: ZohoConnection): Promise<ZohoConnection> {
  return connection(await request(`${root(projectId)}/connections/${encodeURIComponent(row.id)}/enabled`, {
    method: 'POST', body: JSON.stringify({ expected_version: row.version, enabled: !row.enabled }),
  }));
}
export async function proposeZoho(projectId: string, fields: {
  connection_id: string; run_id: string; operation: 'draft' | 'send'; to_address: string; subject: string; body: string;
}): Promise<void> {
  const value = await request(`${root(projectId)}/actions`, { method: 'POST', body: JSON.stringify(fields) });
  if (!value || typeof value !== 'object' || (value as Record<string, unknown>).state !== 'pending') {
    throw new ApiError('Could not confirm the Zoho approval request. Check Approvals before retrying.', null);
  }
}

export async function zohoActionState(projectId: string, approvalId: string): Promise<string> {
  const rows = await request(`${root(projectId)}/actions`);
  if (!Array.isArray(rows)) throw new ApiError('Could not confirm the Zoho action receipt.', null);
  const row = rows.find((value: unknown) => value && typeof value === 'object' &&
    (value as Record<string, unknown>).approval_id === approvalId) as Record<string, unknown> | undefined;
  if (!row || !['pending', 'approved', 'rejected', 'dispatching', 'accepted', 'failed', 'unknown'].includes(String(row.state))) {
    throw new ApiError('Zoho decision saved but receipt is missing. Inspect Activity before another action.', null);
  }
  return String(row.state);
}
