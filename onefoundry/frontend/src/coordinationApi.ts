import { ApiError, request } from './api';
import type { DeliverableKind } from './workApi';

export type CoordinationMode = 'off' | 'draft_only' | 'auto';
export type RuleAutonomy = 'draft' | 'ready' | 'auto_start';
export type { DeliverableKind };

export interface Schedule {
  kind: 'daily' | 'weekly' | 'interval';
  at?: string;
  weekday?: number;
  every_hours?: number;
}

export interface CoordinationStatus {
  mode: CoordinationMode;
  max_chain_depth: number;
  max_orders_per_day: number;
  max_auto_starts_per_day: number;
  timezone: string;
  paused: boolean;
  agent_rules_enabled: boolean;
  version: number;
  signal_types: Record<string, string[]>;
  profile_tool_ceiling: string[];
  registered_tools: string[];
  registered_tool_labels?: Record<string, string>;
}

export interface RuleFields {
  name: string;
  signal_type: string;
  schedule: Schedule | null;
  filters: Record<string, string>;
  target_profile_id: string;
  title: string;
  instructions: string;
  deliverable_kind: DeliverableKind;
  autonomy: RuleAutonomy;
  max_per_day: number;
  max_model_attempts?: number;
  quiet_hours?: { start: string; end: string } | null;
  max_chain_depth: number | null;
}

export interface CoordinationRule extends RuleFields {
  id: string;
  enabled: boolean;
  version: number;
  current_version: number;
  updated_at: string;
  last_agent_action: { turn_id: string | null; run_id: string | null; actor_id: string; decision: string;
    rule_version: number; source_kind: 'operator_ui' | 'discord' | 'wechat' | 'agent_run' } | null;
}

export interface SignalIntent {
  id: string;
  action_kind: string;
  rule_name: string | null;
  rule_version: number | null;
  state: string;
  autonomy: RuleAutonomy | null;
  error_code: string | null;
  decision: { action?: string; reason?: string; run_id?: string } | null;
  work_order_id: string | null;
  work_order_title: string | null;
}

export interface CoordinationSignal {
  id: string;
  source: string;
  signal_type: string;
  fields: Record<string, unknown>;
  origin_work_order_id: string | null;
  chain_depth: number;
  state: 'new' | 'matched' | 'ignored' | 'blocked' | 'triage';
  reason: string | null;
  created_at: string;
  intents: SignalIntent[];
}

function bad(): never {
  throw new ApiError('The server returned an unexpected automation response.', null);
}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) bad();
  return value as Record<string, unknown>;
}

function str(value: unknown): string {
  if (typeof value !== 'string') bad();
  return value;
}

function maybeStr(value: unknown): string | null {
  return value === null || value === undefined ? null : str(value);
}

function int(value: unknown): number {
  if (!Number.isSafeInteger(value)) bad();
  return value as number;
}

function filters(value: unknown): Record<string, string> {
  const result: Record<string, string> = {};
  for (const [key, item] of Object.entries(object(value))) {
    result[key] = Array.isArray(item) ? item.map(str).join(', ') : str(item);
  }
  return result;
}

function status(value: unknown): CoordinationStatus {
  const row = object(value);
  const mode = str(row.mode);
  if (mode !== 'off' && mode !== 'draft_only' && mode !== 'auto') bad();
  const types: Record<string, string[]> = {};
  for (const [key, item] of Object.entries(object(row.signal_types))) {
    if (!Array.isArray(item)) bad();
    types[key] = item.map(str);
  }
  if (typeof row.paused !== 'boolean' || typeof row.agent_rules_enabled !== 'boolean') bad();
  if (!Array.isArray(row.profile_tool_ceiling) || !Array.isArray(row.registered_tools)) bad();
  const labels: Record<string, string> = row.registered_tool_labels === undefined ? {} :
    Object.fromEntries(Object.entries(object(row.registered_tool_labels)).map(([key, value]) => [key, str(value)]));
  return { mode, max_chain_depth: int(row.max_chain_depth), max_orders_per_day: int(row.max_orders_per_day),
    profile_tool_ceiling: row.profile_tool_ceiling.map(str), registered_tools: row.registered_tools.map(str),
    registered_tool_labels: labels,
    max_auto_starts_per_day: int(row.max_auto_starts_per_day), timezone: str(row.timezone), paused: row.paused,
    agent_rules_enabled: row.agent_rules_enabled, version: int(row.version), signal_types: types };
}

function schedule(value: unknown): Schedule | null {
  if (value === null || value === undefined) return null;
  const row = object(value);
  const kind = str(row.kind);
  if (kind !== 'daily' && kind !== 'weekly' && kind !== 'interval') bad();
  return { kind, ...(row.at == null ? {} : { at: str(row.at) }), ...(row.weekday == null ? {} : { weekday: int(row.weekday) }),
    ...(row.every_hours == null ? {} : { every_hours: int(row.every_hours) }) };
}

function rule(value: unknown): CoordinationRule {
  const row = object(value);
  const autonomy = str(row.autonomy);
  if (autonomy !== 'draft' && autonomy !== 'ready' && autonomy !== 'auto_start') bad();
  if (typeof row.enabled !== 'boolean') bad();
  return {
    id: str(row.id), name: str(row.name), enabled: row.enabled, version: int(row.version),
    current_version: int(row.current_version), signal_type: str(row.signal_type), schedule: schedule(row.schedule),
    filters: filters(row.filters),
    target_profile_id: str(row.target_profile_id), title: str(row.title), instructions: str(row.instructions),
    deliverable_kind: str(row.deliverable_kind) as DeliverableKind, autonomy, max_per_day: int(row.max_per_day),
    max_chain_depth: row.max_chain_depth === null ? null : int(row.max_chain_depth), updated_at: str(row.updated_at),
    max_model_attempts: row.max_model_attempts === undefined ? 10 : int(row.max_model_attempts),
    quiet_hours: row.quiet_hours == null ? null : {
      start: str(object(row.quiet_hours).start), end: str(object(row.quiet_hours).end) },
    last_agent_action: row.last_agent_action == null ? null : agentAction(row.last_agent_action),
  };
}

function agentAction(value: unknown): NonNullable<CoordinationRule['last_agent_action']> {
  const row = object(value);
  if (!['operator_ui', 'discord', 'wechat', 'agent_run'].includes(str(row.source_kind))) bad();
  return { turn_id: row.turn_id == null ? null : str(row.turn_id), run_id: row.run_id == null ? null : str(row.run_id),
    actor_id: str(row.actor_id), decision: str(row.decision),
    rule_version: int(row.rule_version), source_kind: row.source_kind as 'operator_ui' | 'discord' | 'wechat' | 'agent_run' };
}

function intent(value: unknown): SignalIntent {
  const row = object(value);
  return {
    id: str(row.id), action_kind: str(row.action_kind), rule_name: maybeStr(row.rule_name), rule_version: row.rule_version === null ? null : int(row.rule_version),
    state: str(row.state), autonomy: maybeStr(row.autonomy) as RuleAutonomy | null, error_code: maybeStr(row.error_code),
    decision: row.decision == null ? null : decision(row.decision),
    work_order_id: maybeStr(row.work_order_id), work_order_title: maybeStr(row.work_order_title),
  };
}

function decision(value: unknown): { action?: string; reason?: string; run_id?: string } {
  const row = object(value);
  return { action: maybeStr(row.action) ?? undefined, reason: maybeStr(row.reason) ?? undefined,
    run_id: maybeStr(row.run_id) ?? undefined };
}

function parseSignal(value: unknown): CoordinationSignal {
  const row = object(value);
  const state = str(row.state);
  if (state !== 'new' && state !== 'matched' && state !== 'ignored' && state !== 'blocked' && state !== 'triage') bad();
  if (!Array.isArray(row.intents)) bad();
  return {
    id: str(row.id), source: str(row.source), signal_type: str(row.signal_type), fields: object(row.fields),
    origin_work_order_id: maybeStr(row.origin_work_order_id), chain_depth: int(row.chain_depth), state,
    reason: maybeStr(row.reason), created_at: str(row.created_at), intents: row.intents.map(intent),
  };
}

const base = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/coordination`;

export async function getCoordination(projectId: string, signal?: AbortSignal): Promise<CoordinationStatus> {
  return status(await request(base(projectId), { signal }));
}

export async function setPaused(projectId: string, paused: boolean, expectedVersion: number): Promise<CoordinationStatus> {
  return status(await request(`${base(projectId)}/${paused ? 'pause' : 'resume'}`, {
    method: 'POST', body: JSON.stringify({ expected_version: expectedVersion }),
  }));
}

export async function setAgentRuleAccess(projectId: string, enabled: boolean, expectedVersion: number): Promise<CoordinationStatus> {
  return status(await request(`${base(projectId)}/agent-rule-access`, {
    method: 'PUT', body: JSON.stringify({ enabled, expected_version: expectedVersion }),
  }));
}

export async function setToolCeiling(projectId: string, tools: string[], expectedVersion: number): Promise<CoordinationStatus> {
  return status(await request(`${base(projectId)}/tool-ceiling`, {
    method: 'PUT', body: JSON.stringify({ tools, expected_version: expectedVersion }),
  }));
}

export async function listRules(projectId: string, signal?: AbortSignal): Promise<CoordinationRule[]> {
  const value = await request(`${base(projectId)}/rules`, { signal });
  if (!Array.isArray(value)) bad();
  return value.map(rule);
}

function body(fields: RuleFields): Record<string, unknown> {
  return { ...fields, acceptance_checks: [] };
}

export async function createRule(projectId: string, fields: RuleFields): Promise<CoordinationRule> {
  return rule(await request(`${base(projectId)}/rules`, { method: 'POST', body: JSON.stringify(body(fields)) }));
}

export async function reviseRule(projectId: string, ruleId: string, fields: RuleFields, expectedVersion: number): Promise<CoordinationRule> {
  return rule(await request(`${base(projectId)}/rules/${encodeURIComponent(ruleId)}`, {
    method: 'PUT', body: JSON.stringify({ ...body(fields), expected_version: expectedVersion }),
  }));
}

export async function setRuleEnabled(projectId: string, ruleId: string, enabled: boolean, expectedVersion: number): Promise<CoordinationRule> {
  return rule(await request(`${base(projectId)}/rules/${encodeURIComponent(ruleId)}/enabled`, {
    method: 'POST', body: JSON.stringify({ enabled, expected_version: expectedVersion }),
  }));
}

export async function listSignals(projectId: string, signal?: AbortSignal): Promise<CoordinationSignal[]> {
  const value = await request(`${base(projectId)}/signals?limit=50`, { signal });
  if (!Array.isArray(value)) bad();
  return value.map(parseSignal);
}
