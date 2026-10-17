import { ApiError } from './api';

export type CapabilityStatus = 'ready' | 'disabled' | 'permission_required' | 'configuration_missing'
  | 'dependency_unavailable' | 'missing_implementation' | 'unknown';
export type CapabilityCheck = {
  operation: string; title: string; owner: string; source_id: string | null; revision: string | null;
  source_name?: string | null;
  status: CapabilityStatus; next_action: 'use' | 'configure' | 'grant_access' | 'reconcile' | 'implement';
  blockers: { kind: Exclude<CapabilityStatus, 'ready'>; code: string; owner: string; message: string; next_step: string }[];
};
export type CapabilityReport = {
  project_id: string; profile_id: string | null; observed_at: string; boundary: string;
  omitted_sources: number; checks: CapabilityCheck[];
};
const kinds = ['disabled', 'permission_required', 'configuration_missing', 'dependency_unavailable', 'unknown', 'missing_implementation'];
const actions = ['use', 'configure', 'grant_access', 'reconcile', 'implement'];
const row = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object';
const string = (value: unknown): value is string => typeof value === 'string' && value.length > 0;
const nullableString = (value: unknown) => value === null || string(value);

export function capabilityReport(value: unknown, project: string): CapabilityReport {
  const invalid = () => { throw new ApiError('Capability readiness could not be confirmed.', null); };
  if (!row(value) || value.project_id !== project || !nullableString(value.profile_id) ||
      !string(value.observed_at) || !/([+-]\d\d:\d\d|Z)$/.test(value.observed_at) || !Number.isFinite(Date.parse(value.observed_at)) ||
      !string(value.boundary) || !Number.isSafeInteger(value.omitted_sources) || (value.omitted_sources as number) < 0 ||
      !Array.isArray(value.checks) || value.checks.length > 50) return invalid();
  for (const check of value.checks) {
    if (!row(check) || !['operation', 'title', 'owner'].every((key) => string(check[key])) ||
        !nullableString(check.source_id) || !(check.revision === null || (string(check.revision) && /^[a-f0-9]{40}$/.test(check.revision))) ||
        ('source_name' in check && !nullableString(check.source_name)) ||
        ![...kinds, 'ready'].includes(String(check.status)) || !actions.includes(String(check.next_action)) ||
        !Array.isArray(check.blockers) || check.blockers.length > 8) return invalid();
    for (const blocker of check.blockers) {
      if (!row(blocker) || !kinds.includes(String(blocker.kind)) ||
          !['code', 'owner', 'message', 'next_step'].every((key) => string(blocker[key]))) return invalid();
    }
    const blockers = check.blockers;
    const status = kinds.find((kind) => blockers.some((blocker: Record<string, unknown>) => blocker.kind === kind)) ?? 'ready';
    const action = status === 'ready' ? 'use' : status === 'permission_required' ? 'grant_access'
      : status === 'unknown' ? 'reconcile' : status === 'missing_implementation' ? 'implement' : 'configure';
    if (check.status !== status || check.next_action !== action) return invalid();
  }
  return value as CapabilityReport;
}
