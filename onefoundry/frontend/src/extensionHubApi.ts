import { ApiError, request } from './api';

export type ExtensionTool = { id: string; declared_id?: string; version: number | string; description: string; source: 'builtin' | 'extension'; status: string; package_id?: string | null };
export type ExtensionSkill = { id: string; name: string; package_id: string; status: string; required_tools: string[] };
export type ExtensionPackage = { id: string; project_id: string; package_key: string; package_version: string; sha256: string; state: string; revision: number; manifest: Record<string, unknown>; created_at: string };
export type ExtensionConnection = { id: string; project_id: string; package_id: string; template_id: string; label: string; config: Record<string, unknown>; enabled: boolean; version: number; created_at: string };
export type ExtensionDraft = { id: string; project_id: string; kind: 'skill'; name: string; description: string; content: string; required_tools: string[]; status: string; sha256: string; source_run_id?: string | null; created_at: string };
export type ExtensionCatalog = { tools: ExtensionTool[]; packages: ExtensionPackage[]; skills: ExtensionSkill[] };
export type ConnectionReadiness = { version: number; credential_present: boolean; configured: boolean; enabled: boolean; live_test_status: string; successful_use: boolean };
export type ExtensionInvocation = { invocation_id: string; tool_id: string; status: string; approval_id: string | null; reason: string | null };
export type PackageFormat = 'onefoundry' | 'codex-portable' | 'codex-legacy' | 'claude';
export type CompatibilityReport = { format: string; components: { component: string; status: 'supported' | 'inactive' | 'rejected'; reason: string }[] };
export type PackageBinding = { package_id: string; slot: string; connection_id: string; version: number };
export type ExtensionWebhook = { id: string; name: string; enabled: boolean; version: number; credential_present: boolean; created_at: string };
export type ExtensionWorkerStatus = { available: boolean; reason: 'not_configured' | 'unavailable' | 'stale' | 'policy_mismatch' | 'ready'; age_seconds?: number };

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ApiError('The extension service returned an unexpected response.', null);
  return value as Record<string, unknown>;
}
function string(value: unknown): string {
  if (typeof value !== 'string') throw new ApiError('The extension service returned an unexpected response.', null);
  return value;
}
function array(value: unknown): unknown[] {
  if (!Array.isArray(value)) throw new ApiError('The extension service returned an unexpected list.', null);
  return value;
}
function packageRecord(value: unknown): ExtensionPackage {
  const item = object(value);
  if (typeof item.revision !== 'number' || !Number.isInteger(item.revision)) throw new ApiError('Invalid package revision.', null);
  return { id: string(item.id), project_id: string(item.project_id), package_key: string(item.package_key), package_version: string(item.package_version),
    sha256: string(item.sha256), state: string(item.state), revision: item.revision,
    manifest: object(item.manifest), created_at: string(item.created_at) };
}
function connectionRecord(value: unknown): ExtensionConnection {
  const item = object(value);
  if (typeof item.enabled !== 'boolean' || typeof item.version !== 'number') throw new ApiError('Invalid connection state.', null);
  return { id: string(item.id), project_id: string(item.project_id), package_id: string(item.package_id), template_id: string(item.template_id),
    label: string(item.label), config: object(item.config), enabled: item.enabled, version: item.version, created_at: string(item.created_at) };
}
function draftRecord(value: unknown): ExtensionDraft {
  const item = object(value);
  if (item.kind !== 'skill') throw new ApiError('Unknown draft kind.', null);
  return { id: string(item.id), project_id: string(item.project_id), kind: 'skill', name: string(item.name), description: string(item.description),
    content: string(item.content), required_tools: array(item.required_tools).map(string), status: string(item.status), sha256: string(item.sha256),
    source_run_id: item.source_run_id == null ? null : string(item.source_run_id), created_at: string(item.created_at) };
}
const base = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/extensions`;
export async function getExtensionWorkerStatus(projectId: string, signal?: AbortSignal): Promise<ExtensionWorkerStatus> {
  const item = object(await request(`${base(projectId)}/worker`, { signal }));
  if (typeof item.available !== 'boolean' || !['not_configured', 'unavailable', 'stale', 'policy_mismatch', 'ready'].includes(String(item.reason)) ||
      item.available !== (item.reason === 'ready') || (item.age_seconds !== undefined && (typeof item.age_seconds !== 'number' || !Number.isFinite(item.age_seconds) || item.age_seconds < 0))) {
    throw new ApiError('Invalid extension worker status.', null);
  }
  return { available: item.available, reason: item.reason as ExtensionWorkerStatus['reason'],
    ...(item.age_seconds === undefined ? {} : { age_seconds: item.age_seconds as number }) };
}
function webhookRecord(value: unknown): ExtensionWebhook {
  const item = object(value);
  if (typeof item.enabled !== 'boolean' || typeof item.credential_present !== 'boolean' || !Number.isInteger(item.version)) throw new ApiError('Invalid webhook state.', null);
  return { id: string(item.id), name: string(item.name), enabled: item.enabled, version: item.version as number,
    credential_present: item.credential_present, created_at: string(item.created_at) };
}
export async function listExtensionWebhooks(projectId: string, signal?: AbortSignal): Promise<ExtensionWebhook[]> {
  return array(await request(`${base(projectId)}/webhooks`, { signal })).map(webhookRecord);
}
export async function createExtensionWebhook(projectId: string, name: string, signingSecret: string): Promise<ExtensionWebhook> {
  return webhookRecord(await request(`${base(projectId)}/webhooks`, { method: 'POST', body: JSON.stringify({ name, signing_secret: signingSecret, enabled: false }) }));
}
export async function updateExtensionWebhook(projectId: string, item: ExtensionWebhook, enabled: boolean): Promise<ExtensionWebhook> {
  return webhookRecord(await request(`${base(projectId)}/webhooks/${encodeURIComponent(item.id)}`, {
    method: 'PATCH', body: JSON.stringify({ expected_version: item.version, enabled }),
  }));
}
export async function rotateExtensionWebhookSecret(projectId: string, item: ExtensionWebhook, value: string): Promise<ExtensionWebhook> {
  return webhookRecord(await request(`${base(projectId)}/webhooks/${encodeURIComponent(item.id)}/credential`, {
    method: 'PUT', body: JSON.stringify({ expected_version: item.version, value }),
  }));
}

export async function listExtensionCatalog(projectId: string, signal?: AbortSignal): Promise<ExtensionCatalog> {
  const item = object(await request(`${base(projectId)}/catalog`, { signal }));
  return { tools: array(item.tools).map((value) => {
    const tool = object(value);
    if (tool.source !== 'builtin' && tool.source !== 'extension') throw new ApiError('Unknown tool source.', null);
    return { id: string(tool.id), ...(tool.declared_id == null ? {} : { declared_id: string(tool.declared_id) }),
      version: typeof tool.version === 'number' ? tool.version : string(tool.version),
      description: string(tool.description), source: tool.source, status: string(tool.status),
      package_id: tool.package_id == null ? null : string(tool.package_id) };
  }), packages: array(item.packages).map(packageRecord), skills: array(item.skills).map((value) => {
    const skill = object(value);
    return { id: string(skill.id), name: string(skill.name), package_id: string(skill.package_id), status: string(skill.status),
      required_tools: array(skill.required_tools).map(string) };
  }) };
}
export async function listExtensionPackages(projectId: string, signal?: AbortSignal): Promise<ExtensionPackage[]> {
  return array(await request(`${base(projectId)}/packages`, { signal })).map(packageRecord);
}
export async function createExtensionPackage(projectId: string, manifest: Record<string, unknown>, files: Record<string, string>): Promise<ExtensionPackage> {
  return packageRecord(await request(`${base(projectId)}/packages`, { method: 'POST', body: JSON.stringify({ manifest, files }) }));
}
export async function importExtensionPackage(projectId: string, format: PackageFormat, archiveBase64: string): Promise<{ package: ExtensionPackage; report: CompatibilityReport }> {
  const result = object(await request(`${base(projectId)}/packages/import`, { method: 'POST', body: JSON.stringify({ format, archive_base64: archiveBase64 }) }));
  const report = object(result.report);
  return { package: packageRecord(result.package), report: { format: string(report.format), components: array(report.components).map((value) => {
    const item = object(value);
    if (item.status !== 'supported' && item.status !== 'inactive' && item.status !== 'rejected') throw new ApiError('Invalid compatibility status.', null);
    return { component: string(item.component), status: item.status, reason: string(item.reason) };
  }) } };
}
export async function exportExtensionPackage(projectId: string, packageId: string, format: PackageFormat): Promise<Blob> {
  let response: Response;
  try { response = await fetch(`${base(projectId)}/packages/${encodeURIComponent(packageId)}/export?format=${encodeURIComponent(format)}`, { credentials: 'same-origin' }); }
  catch { throw new ApiError('Could not reach OneFoundry. Refresh saved state before retrying.', null); }
  if (!response.ok) throw new ApiError(`Package export failed (${response.status}).`, response.status);
  if (!response.headers.get('Content-Type')?.toLowerCase().includes('application/zip')) throw new ApiError('The export was not a ZIP archive.', null);
  return response.blob();
}
function bindingRecord(value: unknown): PackageBinding {
  const item = object(value);
  if (!Number.isInteger(item.version)) throw new ApiError('Invalid binding version.', null);
  return { package_id: string(item.package_id), slot: string(item.slot), connection_id: string(item.connection_id), version: item.version as number };
}
export async function listPackageBindings(projectId: string, packageId: string, signal?: AbortSignal): Promise<PackageBinding[]> {
  return array(await request(`${base(projectId)}/packages/${encodeURIComponent(packageId)}/bindings`, { signal })).map(bindingRecord);
}
export async function putPackageBinding(projectId: string, packageId: string, slot: string, connectionId: string, expectedVersion: number): Promise<PackageBinding> {
  return bindingRecord(await request(`${base(projectId)}/packages/${encodeURIComponent(packageId)}/bindings/${encodeURIComponent(slot)}`, {
    method: 'PUT', body: JSON.stringify({ connection_id: connectionId, expected_version: expectedVersion }),
  }));
}
export async function proposePackageState(projectId: string, item: ExtensionPackage, action: 'activate' | 'revoke'): Promise<string> {
  const result = object(await request(`${base(projectId)}/packages/${encodeURIComponent(item.id)}/${action}`, {
    method: 'POST', body: JSON.stringify({ expected_sha256: item.sha256, expected_version: item.package_version, expected_revision: item.revision }),
  }));
  if (result.status !== 'pending') throw new ApiError('The package proposal did not enter approval.', null);
  return string(result.approval_id);
}
export async function listExtensionConnections(projectId: string, signal?: AbortSignal): Promise<ExtensionConnection[]> {
  return array(await request(`${base(projectId)}/connections`, { signal })).map(connectionRecord);
}
export async function createExtensionConnection(projectId: string, fields: { package_id: string; template_id: string; label: string; config: Record<string, unknown> }): Promise<ExtensionConnection> {
  return connectionRecord(await request(`${base(projectId)}/connections`, { method: 'POST', body: JSON.stringify(fields) }));
}
export async function updateExtensionConnection(projectId: string, item: ExtensionConnection, fields: { enabled?: boolean; config?: Record<string, unknown> }): Promise<ExtensionConnection> {
  return connectionRecord(await request(`${base(projectId)}/connections/${encodeURIComponent(item.id)}`, {
    method: 'PATCH', body: JSON.stringify({ expected_version: item.version, ...fields }),
  }));
}
export async function putExtensionCredential(projectId: string, item: ExtensionConnection, value: string): Promise<{ credential_present: true; version: number }> {
  const result = object(await request(`${base(projectId)}/connections/${encodeURIComponent(item.id)}/credential`, {
    method: 'PUT', body: JSON.stringify({ value, expected_version: item.version }),
  }));
  if (result.credential_present !== true || !Number.isInteger(result.version)) throw new ApiError('Credential write was not confirmed.', null);
  return { credential_present: true, version: result.version as number };
}
export async function getExtensionReadiness(projectId: string, item: ExtensionConnection, signal?: AbortSignal): Promise<ConnectionReadiness> {
  const result = object(await request(`${base(projectId)}/connections/${encodeURIComponent(item.id)}/readiness`, { signal }));
  for (const key of ['credential_present', 'configured', 'enabled', 'successful_use']) if (typeof result[key] !== 'boolean') throw new ApiError('Invalid connection readiness.', null);
  if (!Number.isInteger(result.version)) throw new ApiError('Invalid connection readiness version.', null);
  return { version: result.version as number, credential_present: result.credential_present as boolean, configured: result.configured as boolean,
    enabled: result.enabled as boolean, live_test_status: string(result.live_test_status), successful_use: result.successful_use as boolean };
}
export async function probeExtensionConnection(projectId: string, item: ExtensionConnection, endpoint: string): Promise<unknown> {
  return request(`${base(projectId)}/connections/${encodeURIComponent(item.id)}/probe`, {
    method: 'POST', body: JSON.stringify({ expected_version: item.version, confirm_endpoint: endpoint }),
  });
}
function invocationRecord(value: unknown): ExtensionInvocation {
  const item = object(value);
  return { invocation_id: string(item.invocation_id), tool_id: string(item.tool_id), status: string(item.status),
    approval_id: item.approval_id == null ? null : string(item.approval_id), reason: item.reason == null ? null : string(item.reason) };
}
export async function listExtensionInvocations(projectId: string, signal?: AbortSignal): Promise<ExtensionInvocation[]> {
  return array(await request(`${base(projectId)}/invocations`, { signal })).map(invocationRecord);
}
export async function executeExtensionInvocation(projectId: string, item: ExtensionInvocation): Promise<ExtensionInvocation> {
  return invocationRecord(await request(`${base(projectId)}/invocations/${encodeURIComponent(item.invocation_id)}/execute`, {
    method: 'POST', body: '{}',
  }));
}
export async function cancelExtensionInvocation(projectId: string, item: ExtensionInvocation): Promise<ExtensionInvocation> {
  return invocationRecord(await request(`${base(projectId)}/invocations/${encodeURIComponent(item.invocation_id)}/cancel`, {
    method: 'POST', body: '{}',
  }));
}
export async function listExtensionDrafts(projectId: string, signal?: AbortSignal): Promise<ExtensionDraft[]> {
  return array(await request(`${base(projectId)}/drafts`, { signal })).map(draftRecord);
}
export async function createExtensionDraft(projectId: string, fields: { name: string; description: string; content: string; required_tools: string[]; source_run_id?: string }): Promise<ExtensionDraft> {
  return draftRecord(await request(`${base(projectId)}/drafts`, { method: 'POST', body: JSON.stringify({ kind: 'skill', ...fields }) }));
}
export async function proposeDraftDecision(projectId: string, item: ExtensionDraft, decision: 'accept' | 'reject'): Promise<string> {
  const result = object(await request(`${base(projectId)}/drafts/${encodeURIComponent(item.id)}/decision`, {
    method: 'POST', body: JSON.stringify({ expected_sha256: item.sha256, decision }),
  }));
  const status = string(result.status);
  if (!['accepted', 'rejected', 'pending'].includes(status)) throw new ApiError('Unexpected skill review result.', null);
  return status === 'pending' ? 'Skill decision pending in Approvals.' : `Skill draft ${status} and confirmed.`;
}
