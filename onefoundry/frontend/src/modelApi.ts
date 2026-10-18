import { ApiError, request } from './api';

export type ModelProvider = string;

export interface ProviderDescriptor {
  id: ModelProvider;
  name: string;
  auth_kind: 'api_key' | 'none' | 'account_runtime';
  catalog_supported: boolean;
  text_runtime_available: boolean;
  connectable: boolean;
}

export interface ModelAccount {
  id: string;
  project_id: string;
  provider: ModelProvider;
  label: string;
  auth_kind: 'api_key' | 'none' | 'account_runtime';
  endpoint: string | null;
  credential_present: boolean;
  catalog_valid: boolean;
  model_count: number;
  last_refreshed_at: string | null;
  connection_status?: 'connected' | 'expired' | 'reconnect_required' | 'pending' | 'login_expired' | 'login_incomplete' | null;
}

export interface CatalogModel {
  id: string;
  account_id: string;
  provider: ModelProvider;
  model_id: string;
  display_name: string;
  category: string;
  reasoning_efforts: string[];
  available: boolean;
}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ApiError('Invalid model gateway response.', null);
  return value as Record<string, unknown>;
}

function valueString(value: unknown): string {
  if (typeof value !== 'string') throw new ApiError('Invalid model gateway response.', null);
  return value;
}

function provider(value: unknown): ModelProvider {
  if (typeof value !== 'string' || !/^[a-z][a-z0-9_]{0,63}$/.test(value)) throw new ApiError('Invalid model provider.', null);
  return value;
}

function parseAccount(value: unknown): ModelAccount {
  const item = object(value);
  if (item.connection_status !== undefined && item.connection_status !== null &&
      (typeof item.connection_status !== 'string' || !['connected', 'expired', 'reconnect_required', 'pending', 'login_expired', 'login_incomplete'].includes(item.connection_status))) {
    throw new ApiError('Invalid model connection status.', null);
  }
  if ((item.auth_kind !== 'api_key' && item.auth_kind !== 'none' && item.auth_kind !== 'account_runtime') || (item.endpoint !== null && typeof item.endpoint !== 'string') || typeof item.credential_present !== 'boolean' || typeof item.catalog_valid !== 'boolean' ||
      typeof item.model_count !== 'number' || !Number.isSafeInteger(item.model_count) || item.model_count < 0 ||
      (item.last_refreshed_at !== null && typeof item.last_refreshed_at !== 'string')) {
    throw new ApiError('Invalid model account response.', null);
  }
  return {
    id: valueString(item.id), project_id: valueString(item.project_id), provider: provider(item.provider),
    label: valueString(item.label), auth_kind: item.auth_kind, endpoint: item.endpoint, credential_present: item.credential_present,
    catalog_valid: item.catalog_valid,
    model_count: item.model_count, last_refreshed_at: item.last_refreshed_at,
    connection_status: item.connection_status as ModelAccount['connection_status'],
  };
}

function parseModel(value: unknown): CatalogModel {
  const item = object(value);
  const efforts = item.reasoning_efforts ?? [];
  if (typeof item.available !== 'boolean' || !Array.isArray(efforts) ||
      !efforts.every((effort) => typeof effort === 'string' && /^[a-z][a-z0-9_]{0,31}$/.test(effort))) {
    throw new ApiError('Invalid model catalog response.', null);
  }
  return {
    id: valueString(item.id), account_id: valueString(item.account_id), provider: provider(item.provider),
    model_id: valueString(item.model_id), display_name: valueString(item.display_name),
    category: valueString(item.category), reasoning_efforts: efforts, available: item.available,
  };
}

function list(value: unknown): unknown[] {
  if (!Array.isArray(value)) throw new ApiError('Invalid model gateway list.', null);
  return value;
}

function base(projectId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/model`;
}

export async function listModelProviders(projectId: string, signal?: AbortSignal): Promise<ProviderDescriptor[]> {
  return list(await request(`${base(projectId)}/providers`, { signal })).map((raw) => {
    const item = object(raw);
    if ((item.auth_kind !== 'api_key' && item.auth_kind !== 'none' && item.auth_kind !== 'account_runtime') ||
        typeof item.catalog_supported !== 'boolean' || typeof item.text_runtime_available !== 'boolean' ||
        typeof item.connectable !== 'boolean') {
      throw new ApiError('Invalid provider descriptor.', null);
    }
    return { id: provider(item.id), name: valueString(item.name), auth_kind: item.auth_kind,
      catalog_supported: item.catalog_supported, text_runtime_available: item.text_runtime_available,
      connectable: item.connectable };
  });
}

export async function listModelAccounts(projectId: string, signal?: AbortSignal): Promise<ModelAccount[]> {
  return list(await request(`${base(projectId)}/accounts`, { signal })).map(parseAccount);
}

export async function listCatalogModels(projectId: string, signal?: AbortSignal): Promise<CatalogModel[]> {
  return list(await request(`${base(projectId)}/catalog`, { signal })).map(parseModel);
}

export async function addModelAccount(projectId: string, data: {provider: ModelProvider; label: string; api_key?: string; endpoint?: string}): Promise<ModelAccount> {
  return parseAccount(await request(`${base(projectId)}/accounts`, { method: 'POST', body: JSON.stringify(data) }));
}

export interface CodexChallenge {
  account: ModelAccount;
  verification_url: string;
  user_code: string;
  expires_at: number;
  interval_seconds: number;
}

function parseChallenge(value: unknown): CodexChallenge {
  const item = object(value);
  if (typeof item.expires_at !== 'number' || typeof item.interval_seconds !== 'number') {
    throw new ApiError('Invalid Codex sign-in response.', null);
  }
  return { account: parseAccount(item.account), verification_url: valueString(item.verification_url),
    user_code: valueString(item.user_code), expires_at: item.expires_at, interval_seconds: item.interval_seconds };
}

export async function startCodexConnection(projectId: string, label: string): Promise<CodexChallenge> {
  return parseChallenge(await request(`${base(projectId)}/codex/start`, {
    method: 'POST', body: JSON.stringify({ label }),
  }));
}

export async function finishCodexConnection(projectId: string, accountId: string): Promise<ModelAccount> {
  return parseAccount(await request(`${base(projectId)}/accounts/${encodeURIComponent(accountId)}/codex/finish`, { method: 'POST' }));
}

export async function restartCodexConnection(projectId: string, accountId: string): Promise<CodexChallenge> {
  return parseChallenge(await request(`${base(projectId)}/accounts/${encodeURIComponent(accountId)}/codex/restart`, { method: 'POST' }));
}

export async function reconnectCodexConnection(projectId: string, accountId: string): Promise<CodexChallenge> {
  return parseChallenge(await request(`${base(projectId)}/accounts/${encodeURIComponent(accountId)}/codex/reconnect`, { method: 'POST' }));
}

export async function getCodexChallenge(projectId: string, accountId: string): Promise<CodexChallenge> {
  return parseChallenge(await request(`${base(projectId)}/accounts/${encodeURIComponent(accountId)}/codex/challenge`));
}

export async function startGrokConnection(projectId: string, label: string): Promise<CodexChallenge> {
  return parseChallenge(await request(`${base(projectId)}/grok/start`, {
    method: 'POST', body: JSON.stringify({ label }),
  }));
}

export async function finishGrokConnection(projectId: string, accountId: string): Promise<ModelAccount> {
  return parseAccount(await request(`${base(projectId)}/accounts/${encodeURIComponent(accountId)}/grok/finish`, { method: 'POST' }));
}

export async function getGrokChallenge(projectId: string, accountId: string): Promise<CodexChallenge> {
  return parseChallenge(await request(`${base(projectId)}/accounts/${encodeURIComponent(accountId)}/grok/challenge`));
}

export async function restartGrokConnection(projectId: string, accountId: string): Promise<CodexChallenge> {
  return parseChallenge(await request(`${base(projectId)}/accounts/${encodeURIComponent(accountId)}/grok/restart`, { method: 'POST' }));
}

export async function reconnectGrokConnection(projectId: string, accountId: string): Promise<CodexChallenge> {
  return parseChallenge(await request(`${base(projectId)}/accounts/${encodeURIComponent(accountId)}/grok/reconnect`, { method: 'POST' }));
}

export async function replaceModelCredential(projectId: string, accountId: string, apiKey: string): Promise<ModelAccount> {
  return parseAccount(await request(`${base(projectId)}/accounts/${encodeURIComponent(accountId)}/credential`, {
    method: 'PUT', body: JSON.stringify({ api_key: apiKey }),
  }));
}

export async function refreshModelAccount(projectId: string, accountId: string): Promise<{account: ModelAccount; models: CatalogModel[]}> {
  const result = object(await request(`${base(projectId)}/accounts/${encodeURIComponent(accountId)}/refresh`, { method: 'POST' }));
  return { account: parseAccount(result.account), models: list(result.models).map(parseModel) };
}

export async function removeModelAccount(projectId: string, accountId: string): Promise<void> {
  let response: Response;
  try {
    response = await fetch(`${base(projectId)}/accounts/${encodeURIComponent(accountId)}`, {
      method: 'DELETE', credentials: 'same-origin',
    });
  } catch {
    throw new ApiError('Could not reach OneFoundry. Check saved state before retrying.', null);
  }
  if (!response.ok) {
    let detail = '';
    try {
      const body = object(await response.json());
      if (typeof body.detail === 'string') detail = body.detail;
    } catch { /* A failed DELETE may have no JSON body. */ }
    throw new ApiError(detail || `Request failed (${response.status}). Check saved state before retrying.`, response.status);
  }
}
