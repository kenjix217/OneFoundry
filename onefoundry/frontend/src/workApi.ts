import { ApiError, request } from './api';

export type WorkStatus = 'draft' | 'ready' | 'running' | 'blocked' | 'needs_review' | 'done' | 'failed' | 'unknown';
export type DeliverableKind = 'text_reply' | 'artifact' | 'document' | 'code_change' | 'external_action' | 'operator_review';
export type RunStatus = 'running' | 'done' | 'failed' | 'unknown';
export type ApprovalStatus = 'pending' | 'approved' | 'rejected';
export type ActionType = 'email' | 'publish' | 'merge' | 'deploy' | 'cloud_change' | 'other';

export interface WorkTrigger {
  signal_id: string; signal_type: string; source: string; observed_at: string;
  rule_name: string | null; rule_version: number | null;
}

export interface WorkOrder {
  id: string;
  project_id: string;
  title: string;
  instructions: string;
  profile_id: string;
  deliverable_kind: DeliverableKind;
  acceptance_checks: string[];
  parent_work_order_id: string | null;
  source_run_id: string | null;
  origin_signal_id: string | null;
  chain_depth: number;
  triggered_by?: WorkTrigger | null;
  status: WorkStatus;
  version: number;
  block_reason: string | null;
  blocked_from_status: 'draft' | 'ready' | 'running' | null;
  created_at: string;
}

export interface Run {
  id: string;
  project_id: string;
  work_order_id: string;
  work_order_title: string | null;
  work_order_instructions: string | null;
  profile_id: string;
  profile_model: string | null;
  status: RunStatus;
  mode: 'manual_planning' | 'synthetic_text' | 'synthetic_code' | 'live_text' | 'external_code';
  tokens_used: number | null;
  cost_usd: number | null;
  outcome_reason: string;
  started_at: string;
  finished_at: string | null;
  triggered_by?: WorkTrigger | null;
}

export interface RunEvent {
  id: string;
  project_id: string;
  work_order_id: string;
  run_id: string | null;
  event_type: string;
  actor_id: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface ProfileSnapshot {
  profile_id: string;
  profile_name: string;
  profile_model: string | null;
  profile_reasoning_effort: string | null;
  profile_instructions: string;
  memory_scope: 'company' | 'project';
  allowed_tools: string[];
  autonomy_level: 'manual' | 'supervised' | 'bounded';
  transition_index: number;
  actor_id: string;
  recorded_at: string;
}

export interface SkillSnapshot {
  skill_id: string; skill_name: string; version: number; sha256: string;
  transition_index: number; actor_id: string; created_at: string;
}

export interface RunDetail extends Run { events: RunEvent[]; profile_snapshots: ProfileSnapshot[]; skill_snapshots: SkillSnapshot[] }

export interface SyntheticTextEligibility {
  eligible: boolean;
  reason: string | null;
  fixture: 'research_to_gtm';
  researcher_profile_id: string | null;
  gtm_profile_id: string | null;
}

export interface TextOutput {
  id: string;
  project_id: string;
  run_id: string;
  profile_id: string;
  actor_id: string;
  role: 'Researcher' | 'GTM';
  source_kind: 'synthetic_scripted_sdk';
  configured_model: string | null;
  text: string;
  source_memory_ids: string[];
  source_memories: SourceMemoryCitation[];
  tokens_used: number;
  cost_usd: number;
  created_at: string;
}

export interface SourceMemoryCitation {
  id: string;
  project_id: string;
  scope: 'project' | 'company';
  text: string;
  source_result_id: string;
}

export interface LiveTextOutput {
  id: string;
  project_id: string;
  run_id: string;
  profile_id: string;
  actor_id: string;
  account_id: string;
  provider: string;
  model_id: string;
  text: string;
  text_sha256: string;
  automation_decision?: { decision: string; status: 'none' | 'clarify' | 'saved' | 'rejected' | 'unavailable';
    rule_id: string | null; rule_version: number | null; project_version: number | null; plan_version: number | null;
    assigned_work_order_id: string | null; error_code: string | null } | null;
  source_memory_ids: string[];
  source_memories: SourceMemoryCitation[];
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
  cost_usd_ticks: number | null;
  provider_response_id: string | null;
  created_at: string;
}

export class LiveRunError extends ApiError {
  constructor(message: string, status: number | null, public readonly runId: string | null) {
    super(message, status);
    this.name = 'LiveRunError';
  }
}

export interface Approval {
  id: string;
  project_id: string;
  work_order_id: string;
  run_id: string;
  action_type: ActionType;
  target: string;
  content: string;
  proposal_digest: string;
  status: ApprovalStatus;
  created_at: string;
  decided_at: string | null;
  synthetic_merge: boolean;
  synthetic_sales?: boolean;
  outbound_email?: boolean;
  zoho_mail_operation?: 'draft' | 'send' | 'reply_draft' | 'reply_send' | null;
  github_stage?: boolean;
  github_fetch?: boolean;
  github_snapshot?: boolean;
  github_comment?: boolean;
}

function invalid(field: string): never {
  throw new ApiError(`The server returned an invalid ${field}.`, null);
}

function object(value: unknown, field = 'record'): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) invalid(field);
  return value as Record<string, unknown>;
}

function string(value: unknown, field: string): string {
  return typeof value === 'string' ? value : invalid(field);
}

function nullableString(value: unknown, field: string): string | null {
  return value === null ? null : string(value, field);
}

function number(value: unknown, field: string): number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : invalid(field);
}

function nonnegativeInteger(value: unknown, field: string): number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : invalid(field);
}

function nullableNumber(value: unknown, field: string): number | null {
  return value === null ? null : number(value, field);
}

function nullableTicks(value: unknown): number | null {
  if (value === null) return null;
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : invalid('cost ticks');
}

function oneOf<const T extends readonly string[]>(value: unknown, options: T, field: string): T[number] {
  return typeof value === 'string' && options.includes(value) ? value as T[number] : invalid(field);
}

function array(value: unknown, field: string): unknown[] {
  return Array.isArray(value) ? value : invalid(field);
}

function trigger(value: unknown): WorkTrigger | null {
  if (value == null) return null;
  const row = object(value, 'work trigger');
  return { signal_id: string(row.signal_id, 'signal ID'), signal_type: string(row.signal_type, 'signal type'),
    source: string(row.source, 'signal source'), observed_at: string(row.observed_at, 'signal time'),
    rule_name: nullableString(row.rule_name, 'rule name'), rule_version: nullableNumber(row.rule_version, 'rule version') };
}

function workOrder(value: unknown): WorkOrder {
  const row = object(value, 'work order');
  return {
    id: string(row.id, 'work order ID'), project_id: string(row.project_id, 'project ID'),
    title: string(row.title, 'work order title'), instructions: string(row.instructions, 'work order instructions'),
    profile_id: string(row.profile_id, 'profile ID'),
    deliverable_kind: oneOf(row.deliverable_kind ?? 'operator_review',
      ['text_reply', 'artifact', 'document', 'code_change', 'external_action', 'operator_review'], 'deliverable kind'),
    acceptance_checks: array(row.acceptance_checks ?? [], 'acceptance checks').map((item) => string(item, 'acceptance check')),
    parent_work_order_id: nullableString(row.parent_work_order_id ?? null, 'parent work order'),
    source_run_id: nullableString(row.source_run_id ?? null, 'source run'),
    origin_signal_id: nullableString(row.origin_signal_id ?? null, 'origin signal'),
    chain_depth: nonnegativeInteger(row.chain_depth ?? 0, 'chain depth'),
    triggered_by: trigger(row.triggered_by),
    status: oneOf(row.status, ['draft', 'ready', 'running', 'blocked', 'needs_review', 'done', 'failed', 'unknown'], 'work status'),
    version: nonnegativeInteger(row.version, 'work version'),
    block_reason: nullableString(row.block_reason, 'block reason'),
    blocked_from_status: row.blocked_from_status === null ? null :
      oneOf(row.blocked_from_status, ['draft', 'ready', 'running'], 'blocked from status'),
    created_at: string(row.created_at, 'work order time'),
  };
}

export function parseRun(value: unknown): Run {
  const row = object(value, 'run');
  return {
    id: string(row.id, 'run ID'), project_id: string(row.project_id, 'project ID'),
    work_order_id: string(row.work_order_id, 'work order ID'),
    work_order_title: nullableString(row.work_order_title, 'work order title'),
    work_order_instructions: nullableString(row.work_order_instructions, 'work order instructions'),
    profile_id: string(row.profile_id, 'profile ID'),
    profile_model: nullableString(row.profile_model, 'profile model'),
    status: oneOf(row.status, ['running', 'done', 'failed', 'unknown'], 'run status'),
    mode: oneOf(row.mode, ['manual_planning', 'synthetic_text', 'synthetic_code', 'live_text', 'external_code'], 'run mode'),
    tokens_used: nullableNumber(row.tokens_used, 'token count'),
    cost_usd: nullableNumber(row.cost_usd, 'cost'),
    outcome_reason: string(row.outcome_reason, 'outcome reason'),
    started_at: string(row.started_at, 'run start time'),
    finished_at: nullableString(row.finished_at, 'run finish time'),
    triggered_by: trigger(row.triggered_by),
  };
}

function syntheticEligibility(value: unknown): SyntheticTextEligibility {
  const row = object(value, 'synthetic text eligibility');
  if (typeof row.eligible !== 'boolean') invalid('synthetic text eligibility');
  return {
    eligible: row.eligible, reason: nullableString(row.reason, 'eligibility reason'),
    fixture: oneOf(row.fixture, ['research_to_gtm'], 'fixture'),
    researcher_profile_id: nullableString(row.researcher_profile_id, 'Researcher profile ID'),
    gtm_profile_id: nullableString(row.gtm_profile_id, 'GTM profile ID'),
  };
}

function textOutput(value: unknown): TextOutput {
  const row = object(value, 'text output');
  if (row.source_kind !== 'synthetic_scripted_sdk') invalid('text output source');
  return {
    id: string(row.id, 'text output ID'), project_id: string(row.project_id, 'project ID'),
    run_id: string(row.run_id, 'run ID'), profile_id: string(row.profile_id, 'profile ID'),
    actor_id: string(row.actor_id, 'output actor'),
    role: oneOf(row.role, ['Researcher', 'GTM'], 'output role'),
    source_kind: row.source_kind, configured_model: nullableString(row.configured_model, 'configured model'),
    text: string(row.text, 'output text'),
    source_memory_ids: array(row.source_memory_ids, 'source memory IDs').map((id) => string(id, 'source memory ID')),
    source_memories: array(row.source_memories, 'source memory citations').map((value) => {
      const citation = object(value, 'source memory citation');
      return {
        id: string(citation.id, 'source memory ID'), project_id: string(citation.project_id, 'source project ID'),
        scope: oneOf(citation.scope, ['project', 'company'], 'source memory scope'),
        text: string(citation.text, 'source memory text'),
        source_result_id: string(citation.source_result_id, 'source result ID'),
      };
    }),
    tokens_used: number(row.tokens_used, 'output token count'), cost_usd: number(row.cost_usd, 'output cost'),
    created_at: string(row.created_at, 'output time'),
  };
}

function scopedTextOutputs(outputs: TextOutput[], projectId: string, runId: string): TextOutput[] {
  if (outputs.some((output) => output.project_id !== projectId || output.run_id !== runId ||
    output.source_memories.some((source) => (source.scope === 'project' && source.project_id !== projectId) ||
      !output.source_memory_ids.includes(source.id)))) invalid('text output scope');
  return outputs;
}

function liveTextOutput(value: unknown, projectId: string, runId: string): LiveTextOutput {
  const row = object(value, 'live text output');
  const decision = row.automation_decision == null ? null : object(row.automation_decision, 'automation decision');
  const sourceMemoryIds = array(row.source_memory_ids, 'source memory IDs').map((id) => string(id, 'source memory ID'));
  const sources = array(row.source_memories, 'source memory citations').map((value) => {
    const citation = object(value, 'source memory citation');
    return {
      id: string(citation.id, 'source memory ID'), project_id: string(citation.project_id, 'source project ID'),
      scope: oneOf(citation.scope, ['project', 'company'], 'source memory scope'),
      text: string(citation.text, 'source memory text'),
      source_result_id: string(citation.source_result_id, 'source result ID'),
    };
  });
  const output: LiveTextOutput = {
    id: string(row.id, 'output ID'), project_id: string(row.project_id, 'project ID'),
    run_id: string(row.run_id, 'run ID'), profile_id: string(row.profile_id, 'profile ID'),
    actor_id: string(row.actor_id, 'actor ID'), account_id: string(row.account_id, 'account ID'),
    provider: string(row.provider, 'provider'), model_id: string(row.model_id, 'model ID'),
    text: string(row.text, 'output text'), text_sha256: string(row.text_sha256, 'output hash'),
    automation_decision: decision == null ? null : {
      decision: string(decision.decision, 'decision'),
      status: oneOf(decision.status, ['none', 'clarify', 'saved', 'rejected', 'unavailable'], 'decision status'),
      rule_id: nullableString(decision.rule_id, 'rule ID'),
      rule_version: decision.rule_version == null ? null : number(decision.rule_version, 'rule version'),
      project_version: decision.project_version == null ? null : number(decision.project_version, 'project version'),
      plan_version: decision.plan_version == null ? null : number(decision.plan_version, 'plan version'),
      assigned_work_order_id: decision.assigned_work_order_id == null ? null :
        string(decision.assigned_work_order_id, 'assigned work order ID'),
      error_code: nullableString(decision.error_code, 'decision error'),
    },
    source_memory_ids: sourceMemoryIds, source_memories: sources,
    input_tokens: nullableNumber(row.input_tokens, 'input tokens'),
    output_tokens: nullableNumber(row.output_tokens, 'output tokens'),
    cost_usd: nullableNumber(row.cost_usd, 'live output cost'),
    cost_usd_ticks: nullableTicks(row.cost_usd_ticks),
    provider_response_id: nullableString(row.provider_response_id, 'provider response ID'),
    created_at: string(row.created_at, 'output time'),
  };
  if (output.project_id !== projectId || output.run_id !== runId ||
      sources.some((source) => (source.scope === 'project' && source.project_id !== projectId) ||
        !sourceMemoryIds.includes(source.id))) invalid('live output scope');
  return output;
}

export function parseRunEvent(value: unknown): RunEvent {
  const row = object(value, 'run event');
  return {
    id: string(row.id, 'event ID'), project_id: string(row.project_id, 'project ID'),
    work_order_id: string(row.work_order_id, 'work order ID'),
    run_id: nullableString(row.run_id, 'run ID'), event_type: string(row.event_type, 'event type'),
    actor_id: string(row.actor_id, 'event actor'), payload: object(row.payload, 'event details'),
    created_at: string(row.created_at, 'event time'),
  };
}

export async function getWorkOrderActivity(projectId: string, orderId: string,
  signal?: AbortSignal): Promise<RunEvent[]> {
  const rows = array(await request(`${projectPath(projectId)}/work-orders/${encodeURIComponent(orderId)}/activity`,
    { signal }), 'work order activity').map(parseRunEvent);
  if (rows.some((row) => row.project_id !== projectId || row.work_order_id !== orderId)) invalid('work order activity scope');
  return rows;
}

function approval(value: unknown): Approval {
  const row = object(value, 'approval');
  return {
    id: string(row.id, 'approval ID'), project_id: string(row.project_id, 'project ID'),
    work_order_id: string(row.work_order_id, 'work order ID'), run_id: string(row.run_id, 'run ID'),
    action_type: oneOf(row.action_type, ['email', 'publish', 'merge', 'deploy', 'cloud_change', 'other'], 'action type'),
    target: string(row.target, 'approval target'), content: string(row.content, 'approval content'),
    proposal_digest: string(row.proposal_digest, 'proposal digest'),
    status: oneOf(row.status, ['pending', 'approved', 'rejected'], 'approval status'),
    created_at: string(row.created_at, 'approval time'),
    decided_at: nullableString(row.decided_at, 'decision time'),
    synthetic_merge: row.synthetic_merge === true ? true : row.synthetic_merge === false ? false : invalid('synthetic merge marker'),
    synthetic_sales: row.synthetic_sales === true,
    outbound_email: row.outbound_email === true,
    zoho_mail_operation: row.zoho_mail_operation == null ? null :
      oneOf(row.zoho_mail_operation, ['draft', 'send', 'reply_draft', 'reply_send'], 'Zoho mail operation'),
    github_stage: row.github_stage === undefined ? false :
      row.github_stage === true ? true : row.github_stage === false ? false : invalid('GitHub stage marker'),
    github_fetch: row.github_fetch === undefined ? false :
      row.github_fetch === true ? true : row.github_fetch === false ? false : invalid('GitHub fixture fetch marker'),
    github_snapshot: row.github_snapshot === undefined ? false :
      row.github_snapshot === true ? true : row.github_snapshot === false ? false : invalid('GitHub fixture snapshot marker'),
    github_comment: row.github_comment === undefined ? false :
      row.github_comment === true ? true : row.github_comment === false ? false : invalid('GitHub PR comment marker'),
  };
}

export function parseApproval(value: unknown): Approval {
  return approval(value);
}

function projectPath(projectId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}`;
}

function jsonBody(value: unknown): RequestInit {
  return { method: 'POST', body: JSON.stringify(value) };
}

export async function listWorkOrders(projectId: string, signal?: AbortSignal): Promise<WorkOrder[]> {
  return array(await request(`${projectPath(projectId)}/work-orders`, { signal }), 'work order list').map(workOrder);
}

export async function createWorkOrder(projectId: string, fields: { title: string; instructions: string; profile_id: string;
  deliverable_kind?: DeliverableKind; acceptance_checks?: string[] }): Promise<WorkOrder> {
  return workOrder(await request(`${projectPath(projectId)}/work-orders`, jsonBody(fields)));
}

export async function editUnknownWorkOrder(projectId: string, workId: string,
  fields: { title: string; instructions: string; expected_version: number }): Promise<WorkOrder> {
  return workOrder(await request(`${projectPath(projectId)}/work-orders/${encodeURIComponent(workId)}`, {
    method: 'PUT', body: JSON.stringify(fields),
  }));
}

export async function draftQaReviewWorkOrder(projectId: string, runId: string, requestEventId: string,
  fields: { title: string; instructions: string }): Promise<WorkOrder> {
  const order = workOrder(await request(`${projectPath(projectId)}/runs/${encodeURIComponent(runId)}/qa-review-requests/${encodeURIComponent(requestEventId)}/draft`, jsonBody(fields)));
  if (order.project_id !== projectId || order.status !== 'draft') invalid('QA draft scope or status');
  return order;
}

export async function getQaReviewWorkOrder(projectId: string, runId: string, requestEventId: string,
  signal?: AbortSignal): Promise<WorkOrder | null> {
  const value = await request(`${projectPath(projectId)}/runs/${encodeURIComponent(runId)}/qa-review-requests/${encodeURIComponent(requestEventId)}/draft`, { signal });
  if (value === null) return null;
  const order = workOrder(value);
  if (order.project_id !== projectId) invalid('QA work order scope');
  return order;
}

export async function markWorkReady(projectId: string, workId: string): Promise<WorkOrder> {
  return workOrder(await request(`${projectPath(projectId)}/work-orders/${encodeURIComponent(workId)}/ready`, jsonBody({})));
}

export async function reviewWorkOrder(projectId: string, workId: string,
  action: 'accept' | 'return' | 'reopen', reason: string, expectedVersion: number): Promise<WorkOrder> {
  return workOrder(await request(`${projectPath(projectId)}/work-orders/${encodeURIComponent(workId)}/${action}`,
    jsonBody({ reason, expected_version: expectedVersion })));
}

export async function assignWorkOrder(projectId: string, workId: string, profileId: string, expectedVersion: number): Promise<WorkOrder> {
  return workOrder(await request(`${projectPath(projectId)}/work-orders/${encodeURIComponent(workId)}/assign`,
    jsonBody({ profile_id: profileId, expected_version: expectedVersion })));
}

export async function blockWorkOrder(projectId: string, workId: string, reason: string, expectedVersion: number): Promise<WorkOrder> {
  return workOrder(await request(`${projectPath(projectId)}/work-orders/${encodeURIComponent(workId)}/block`,
    jsonBody({ reason, expected_version: expectedVersion })));
}

export async function unblockWorkOrder(projectId: string, workId: string, expectedVersion: number): Promise<WorkOrder> {
  return workOrder(await request(`${projectPath(projectId)}/work-orders/${encodeURIComponent(workId)}/unblock`,
    jsonBody({ expected_version: expectedVersion })));
}

export async function startManualRun(projectId: string, workId: string): Promise<Run> {
  return parseRun(await request(`${projectPath(projectId)}/work-orders/${encodeURIComponent(workId)}/runs`, jsonBody({})));
}

export async function getSyntheticTextEligibility(projectId: string, workId: string, signal?: AbortSignal): Promise<SyntheticTextEligibility> {
  return syntheticEligibility(await request(`${projectPath(projectId)}/work-orders/${encodeURIComponent(workId)}/synthetic-text-eligibility`, { signal }));
}

export async function runSyntheticText(projectId: string, workId: string): Promise<{ run: Run; outputs: TextOutput[] }> {
  const row = object(await request(`${projectPath(projectId)}/work-orders/${encodeURIComponent(workId)}/synthetic-text-run`,
    jsonBody({ fixture: 'research_to_gtm' })), 'synthetic text run');
  const savedRun = parseRun(row.run);
  if (savedRun.mode !== 'synthetic_text' || savedRun.project_id !== projectId || savedRun.work_order_id !== workId) invalid('synthetic run scope');
  const outputs = array(row.outputs, 'text output list').map(textOutput);
  return { run: savedRun, outputs: scopedTextOutputs(outputs, projectId, savedRun.id) };
}

export async function getTextOutputs(projectId: string, runId: string, signal?: AbortSignal): Promise<TextOutput[]> {
  const outputs = array(await request(`${projectPath(projectId)}/runs/${encodeURIComponent(runId)}/text-outputs`, { signal }),
    'text output list').map(textOutput);
  return scopedTextOutputs(outputs, projectId, runId);
}

export async function runLiveText(projectId: string, workId: string,
  restart?: { prior_run_id: string; expected_version: number; acknowledge_unknown: true }): Promise<{run: Run; output: LiveTextOutput}> {
  let response: Response;
  try {
    const action = restart ? 'live-text-restart' : 'live-text-run';
    response = await fetch(`${projectPath(projectId)}/work-orders/${encodeURIComponent(workId)}/${action}`, {
      method: 'POST', body: JSON.stringify(restart ?? {}),
      headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin',
    });
  } catch {
    throw new LiveRunError('Could not confirm the live run outcome. Inspect saved runs before retrying.', null, null);
  }
  let body: unknown;
  try { body = await response.json(); }
  catch { throw new LiveRunError('The live run response was unreadable. Inspect saved runs before retrying.', response.status, null); }
  if (!response.ok) {
    const failure = object(body, 'live run failure');
    const runId = typeof failure.run_id === 'string' ? failure.run_id : null;
    const detail = typeof failure.detail === 'string' ? failure.detail : `Live run failed (${response.status}).`;
    throw new LiveRunError(`${detail} Inspect saved run state before retrying.`, response.status, runId);
  }
  const row = object(body, 'live text run');
  const savedRun = parseRun(row.run);
  if (savedRun.mode !== 'live_text' || savedRun.project_id !== projectId || savedRun.work_order_id !== workId) invalid('live run scope');
  return { run: savedRun, output: liveTextOutput(row.output, projectId, savedRun.id) };
}

export async function getLiveTextOutput(projectId: string, runId: string, signal?: AbortSignal): Promise<LiveTextOutput | null> {
  const result = await request(`${projectPath(projectId)}/runs/${encodeURIComponent(runId)}/live-text-output`, { signal });
  return result === null ? null : liveTextOutput(result, projectId, runId);
}

export async function listRuns(projectId: string, query = '', signal?: AbortSignal): Promise<Run[]> {
  const search = query.trim() ? `?query=${encodeURIComponent(query.trim())}` : '';
  return array(await request(`${projectPath(projectId)}/runs${search}`, { signal }), 'run list').map(parseRun);
}

export async function getRun(projectId: string, runId: string, signal?: AbortSignal): Promise<RunDetail> {
  const row = object(await request(`${projectPath(projectId)}/runs/${encodeURIComponent(runId)}`, { signal }), 'run detail');
  return { ...parseRun(row), events: array(row.events, 'event list').map(parseRunEvent),
    profile_snapshots: array(row.profile_snapshots ?? [], 'profile snapshots').map((value) => {
      const snapshot = object(value, 'profile snapshot');
      return {
        profile_id: string(snapshot.profile_id, 'snapshot profile ID'),
        profile_name: string(snapshot.profile_name, 'snapshot profile name'),
        profile_model: nullableString(snapshot.profile_model, 'snapshot model'),
        profile_reasoning_effort: nullableString(snapshot.profile_reasoning_effort ?? null, 'snapshot reasoning effort'),
        profile_instructions: string(snapshot.profile_instructions, 'snapshot instructions'),
        memory_scope: oneOf(snapshot.memory_scope, ['company', 'project'], 'snapshot memory scope'),
        allowed_tools: array(snapshot.allowed_tools, 'snapshot tools').map((tool) => string(tool, 'snapshot tool')),
        autonomy_level: oneOf(snapshot.autonomy_level, ['manual', 'supervised', 'bounded'], 'snapshot autonomy'),
        transition_index: number(snapshot.transition_index, 'snapshot index'),
        actor_id: string(snapshot.actor_id, 'snapshot actor'),
        recorded_at: string(snapshot.recorded_at, 'snapshot time'),
      };
    }),
    skill_snapshots: array(row.skill_snapshots ?? [], 'skill snapshots').map((value) => {
      const snapshot = object(value, 'skill snapshot');
      return { skill_id: string(snapshot.skill_id, 'snapshot skill ID'),
        skill_name: string(snapshot.skill_name, 'snapshot skill name'),
        version: number(snapshot.version, 'snapshot skill version'),
        sha256: string(snapshot.sha256, 'snapshot skill hash'),
        transition_index: number(snapshot.transition_index, 'snapshot skill assignment'),
        actor_id: string(snapshot.actor_id, 'snapshot skill actor'),
        created_at: string(snapshot.created_at, 'snapshot skill time') };
    }) };
}

export async function handoffRun(projectId: string, runId: string, profileId: string): Promise<Run> {
  return parseRun(await request(`${projectPath(projectId)}/runs/${encodeURIComponent(runId)}/handoffs`, jsonBody({ profile_id: profileId })));
}

export async function finishRun(projectId: string, runId: string, outcome: Exclude<RunStatus, 'running'>, reason: string): Promise<Run> {
  return parseRun(await request(`${projectPath(projectId)}/runs/${encodeURIComponent(runId)}/finish`, jsonBody({ outcome, reason })));
}

export async function listApprovals(projectId: string, status?: ApprovalStatus, signal?: AbortSignal): Promise<Approval[]> {
  const search = status ? `?status=${status}` : '';
  return array(await request(`${projectPath(projectId)}/approvals${search}`, { signal }), 'approval list').map(approval);
}

export async function requestApproval(projectId: string, runId: string, fields: { action_type: ActionType; target: string; content: string }): Promise<Approval> {
  return approval(await request(`${projectPath(projectId)}/runs/${encodeURIComponent(runId)}/approval-requests`, jsonBody(fields)));
}

export async function decideApproval(projectId: string, approvalId: string, decision: Exclude<ApprovalStatus, 'pending'>): Promise<Approval> {
  return approval(await request(`${projectPath(projectId)}/approvals/${encodeURIComponent(approvalId)}/decision`, jsonBody({ decision })));
}
