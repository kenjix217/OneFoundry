import { ApiError, request } from './api';

export type ConversationStatus = 'open' | 'busy' | 'unknown';
export type ConversationTurnStatus = 'running' | 'done' | 'failed' | 'unknown';

export interface TurnAutomationDecision {
  decision: string;
  status: 'none' | 'clarify' | 'saved' | 'rejected' | 'unavailable';
  rule_id: string | null;
  rule_version: number | null;
  project_version: number | null;
  plan_version: number | null;
  assigned_work_order_id: string | null;
  error_code: string | null;
  assignments: { id: string; title: string; status: string; profile_name: string }[];
  validation_errors?: { field: string; code: string }[];
}

export interface TurnSkillSelection {
  skill_id: string;
  skill_name: string | null;
  version: number;
  sha256: string;
  selection_source: 'pinned' | 'automatic';
}

export interface ConversationTurn {
  id: string;
  client_turn_id: string;
  sequence: number;
  status: ConversationTurnStatus;
  discussion_only?: boolean;
  source_kind: 'operator_ui' | 'discord' | 'wechat' | 'system_triage' | 'system_followup';
  automation_decision: TurnAutomationDecision | null;
  operator_text: string;
  assistant_text: string | null;
  assistant_actor_id: string | null;
  reason: string | null;
  diagnostic_code: string | null;
  progress_updates: string[];
  provider: string;
  model_id: string;
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
  selected_skills: TurnSkillSelection[];
  attachments: { id: string; display_name: string; sha256: string }[];
  used_sections: { file_id: string; file_name: string; file_sha256: string;
    kind: 'text' | 'visual'; source_label: string; chunk_sha256: string; byte_count: number }[];
  input_sections_omitted: number;
  created_at: string;
  finished_at: string | null;
}

export interface Conversation {
  id: string;
  project_id: string;
  work_order_id: string;
  profile_id: string;
  title: string;
  is_active: boolean;
  status: ConversationStatus;
  turns: ConversationTurn[];
  turns_omitted?: number;
  latest_sequence?: number;
}

export interface ConversationSummary {
  id: string;
  project_id: string;
  work_order_id: string;
  profile_id: string;
  title: string;
  is_active: boolean;
  status: ConversationStatus;
  created_at: string;
}

function invalid(field: string): never {
  throw new ApiError(`The server returned an invalid ${field}.`, null);
}

function object(value: unknown, field: string): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown> : invalid(field);
}

function string(value: unknown, field: string): string {
  return typeof value === 'string' ? value : invalid(field);
}

function nullableString(value: unknown, field: string): string | null {
  return value === null ? null : string(value, field);
}

function nullableNumber(value: unknown, field: string): number | null {
  if (value === null) return null;
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : invalid(field);
}

function selectedSkill(value: unknown): TurnSkillSelection {
  const row = object(value, 'selected skill');
  if (!Number.isSafeInteger(row.version) || (row.version as number) < 1) invalid('selected skill version');
  const sha256 = string(row.sha256, 'selected skill hash');
  if (!/^[0-9a-f]{64}$/.test(sha256)) invalid('selected skill hash');
  if (row.selection_source !== 'pinned' && row.selection_source !== 'automatic') invalid('selected skill source');
  return { skill_id: string(row.skill_id, 'selected skill ID'),
    skill_name: row.skill_name === undefined ? null : nullableString(row.skill_name, 'selected skill name'),
    version: row.version as number,
    sha256, selection_source: row.selection_source };
}

function automationDecision(value: unknown): TurnAutomationDecision | null {
  if (value === null || value === undefined) return null;
  const row = object(value, 'automation decision');
  const status = string(row.status, 'automation decision status');
  if (!['none', 'clarify', 'saved', 'rejected', 'unavailable'].includes(status)) invalid('automation decision status');
  const assignments = row.assignments === undefined ? [] : Array.isArray(row.assignments)
    ? row.assignments.map((value) => {
      const item = object(value, 'team assignment');
      return { id: string(item.id, 'assignment ID'), title: string(item.title, 'assignment title'),
        status: string(item.status, 'assignment status'), profile_name: string(item.profile_name, 'assigned agent') };
    }) : invalid('team assignments');
  return { decision: string(row.decision, 'automation decision kind'), status: status as TurnAutomationDecision['status'],
    validation_errors: row.validation_errors === undefined ? [] : Array.isArray(row.validation_errors)
      ? row.validation_errors.map((value) => { const issue = object(value, 'proposal validation issue');
        return { field: string(issue.field, 'proposal field'), code: string(issue.code, 'proposal validation code') }; })
      : invalid('proposal validation issues'),
    rule_id: nullableString(row.rule_id, 'automation rule ID'),
    rule_version: row.rule_version === null ? null : nullableNumber(row.rule_version, 'automation rule version'),
    project_version: row.project_version === null ? null : nullableNumber(row.project_version, 'project version'),
    plan_version: row.plan_version == null ? null : nullableNumber(row.plan_version, 'plan version'),
    assigned_work_order_id: row.assigned_work_order_id == null ? null : string(row.assigned_work_order_id, 'assigned work order ID'),
    error_code: nullableString(row.error_code, 'automation error'), assignments };
}

function turn(value: unknown): ConversationTurn {
  const row = object(value, 'conversation turn');
  if (!Number.isSafeInteger(row.sequence) || (row.sequence as number) < 1) invalid('turn sequence');
  if (!['running', 'done', 'failed', 'unknown'].includes(String(row.status))) invalid('turn status');
  return {
    id: string(row.id, 'turn ID'), client_turn_id: string(row.client_turn_id, 'client turn ID'),
    sequence: row.sequence as number, status: row.status as ConversationTurnStatus,
    discussion_only: row.discussion_only === undefined ? false : typeof row.discussion_only === 'boolean'
      ? row.discussion_only : invalid('conversation intent'),
    source_kind: row.source_kind === undefined ? 'operator_ui' :
      ['operator_ui', 'discord', 'wechat', 'system_triage', 'system_followup'].includes(String(row.source_kind))
        ? row.source_kind as ConversationTurn['source_kind'] : invalid('conversation source'),
    automation_decision: automationDecision(row.automation_decision),
    operator_text: string(row.operator_text, 'operator text'),
    assistant_text: nullableString(row.assistant_text, 'assistant text'),
    assistant_actor_id: nullableString(row.assistant_actor_id, 'assistant actor'),
    reason: nullableString(row.reason, 'turn reason'),
    diagnostic_code: row.diagnostic_code === undefined ? null : nullableString(row.diagnostic_code, 'turn diagnostic'),
    progress_updates: row.progress_updates === undefined ? [] : Array.isArray(row.progress_updates) && row.progress_updates.length <= 8
      ? row.progress_updates.map((value) => string(value, 'agent progress summary')) : invalid('agent progress summaries'),
    provider: string(row.provider, 'provider'), model_id: string(row.model_id, 'model ID'),
    input_tokens: nullableNumber(row.input_tokens, 'input token count'),
    output_tokens: nullableNumber(row.output_tokens, 'output token count'),
    cost_usd: nullableNumber(row.cost_usd, 'cost'),
    selected_skills: row.selected_skills === undefined ? [] : Array.isArray(row.selected_skills)
      ? row.selected_skills.map(selectedSkill) : invalid('selected skills'),
    attachments: row.attachments === undefined ? [] : Array.isArray(row.attachments)
      ? row.attachments.map((value) => {
        const item = object(value, 'attachment');
        return { id: string(item.id, 'attachment ID'), display_name: string(item.display_name, 'attachment name'),
          sha256: string(item.sha256, 'attachment hash') };
      }) : invalid('attachments'),
    used_sections: row.used_sections === undefined ? [] : Array.isArray(row.used_sections)
      ? row.used_sections.map((value) => {
        const item = object(value, 'used section');
        const kind = item.kind === undefined ? 'text' : item.kind;
        if (kind !== 'text' && kind !== 'visual') invalid('section kind');
        return { file_id: string(item.file_id, 'section file ID'), file_name: string(item.file_name, 'section file'),
          file_sha256: string(item.file_sha256, 'section file hash'), kind,
          source_label: string(item.source_label, 'section label'),
          chunk_sha256: string(item.chunk_sha256, 'section hash'), byte_count: Number(item.byte_count) };
      }) : invalid('used sections'),
    input_sections_omitted: row.input_sections_omitted === undefined ? 0 : Number(row.input_sections_omitted),
    created_at: string(row.created_at, 'turn time'), finished_at: nullableString(row.finished_at, 'turn finish time'),
  };
}

function conversation(value: unknown, projectId: string, orderId: string): Conversation {
  const row = object(value, 'conversation');
  if (row.project_id !== projectId || row.work_order_id !== orderId) invalid('conversation owner');
  if (!['open', 'busy', 'unknown'].includes(String(row.status))) invalid('conversation status');
  if (!Array.isArray(row.turns)) invalid('conversation turns');
  const turns = row.turns.map(turn);
  const omitted = row.turns_omitted ?? 0;
  const latest = row.latest_sequence ?? turns.at(-1)?.sequence ?? 0;
  if (!Number.isSafeInteger(omitted) || (omitted as number) < 0 ||
      !Number.isSafeInteger(latest) || (latest as number) < (turns.at(-1)?.sequence ?? 0) ||
      turns.some((item, index) => item.sequence !== (omitted as number) + index + 1)) invalid('turn sequence');
  return { id: string(row.id, 'conversation ID'), project_id: projectId, work_order_id: orderId,
    profile_id: string(row.profile_id, 'conversation profile'),
    title: row.title === undefined ? 'Main chat' : string(row.title, 'conversation title'),
    is_active: row.is_active === undefined ? true : typeof row.is_active === 'boolean'
      ? row.is_active : invalid('conversation active state'),
    status: row.status as ConversationStatus, turns, turns_omitted: omitted as number, latest_sequence: latest as number };
}

function path(projectId: string, orderId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/work-orders/${encodeURIComponent(orderId)}/conversation`;
}

function historyPath(projectId: string, orderId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/work-orders/${encodeURIComponent(orderId)}/conversations`;
}

export async function listConversations(projectId: string, orderId: string, signal?: AbortSignal): Promise<ConversationSummary[]> {
  const result: unknown = await request(historyPath(projectId, orderId), { signal });
  if (!Array.isArray(result)) invalid('conversation history');
  return result.map((value) => {
    const row = object(value, 'conversation summary');
    if (row.project_id !== projectId || row.work_order_id !== orderId) invalid('conversation summary owner');
    if (!['open', 'busy', 'unknown'].includes(String(row.status))) invalid('conversation summary status');
    return { id: string(row.id, 'conversation ID'), project_id: projectId, work_order_id: orderId,
      profile_id: string(row.profile_id, 'conversation profile'),
      title: row.title === undefined ? 'Main chat' : string(row.title, 'conversation title'),
      is_active: row.is_active === undefined ? true : typeof row.is_active === 'boolean'
        ? row.is_active : invalid('conversation active state'),
      status: row.status as ConversationStatus,
      created_at: string(row.created_at, 'conversation time') };
  });
}

export async function getConversationById(projectId: string, orderId: string, conversationId: string,
  signal?: AbortSignal, beforeSequence?: number): Promise<Conversation> {
  const query = beforeSequence === undefined ? '' : `?before_sequence=${encodeURIComponent(beforeSequence)}`;
  const saved = conversation(await request(`${historyPath(projectId, orderId)}/${encodeURIComponent(conversationId)}${query}`,
    { signal }), projectId, orderId);
  if (saved.id !== conversationId) invalid('conversation ID');
  return saved;
}

export async function getConversation(projectId: string, orderId: string, signal?: AbortSignal): Promise<Conversation> {
  return conversation(await request(path(projectId, orderId), { signal }), projectId, orderId);
}

export async function openConversation(projectId: string, orderId: string): Promise<Conversation> {
  return conversation(await request(path(projectId, orderId), { method: 'POST', body: '{}' }), projectId, orderId);
}

export async function createConversation(projectId: string, orderId: string, id: string, title: string): Promise<Conversation> {
  const saved = conversation(await request(historyPath(projectId, orderId), {
    method: 'POST', body: JSON.stringify({ id, title }),
  }), projectId, orderId);
  if (saved.id !== id || saved.title !== title) invalid('created conversation');
  return saved;
}

export async function acknowledgeUnknownTurn(projectId: string, orderId: string, turnId: string): Promise<Conversation> {
  const saved = conversation(await request(`${path(projectId, orderId)}/turns/${encodeURIComponent(turnId)}/acknowledge-unknown`,
    { method: 'POST' }), projectId, orderId);
  if (!saved.turns.some((item) => item.id === turnId && item.status === 'unknown')) invalid('acknowledged turn');
  return saved;
}

export async function sendConversationTurn(projectId: string, orderId: string, input: {
  conversation_id: string; client_turn_id: string; expected_sequence: number; text: string; attachment_ids?: string[];
  discussion_only?: boolean;
}): Promise<{ conversation: Conversation; turn: ConversationTurn }> {
  const row = object(await request(`${path(projectId, orderId)}/turns`, {
    method: 'POST', body: JSON.stringify(input),
  }), 'turn result');
  const saved = conversation(row.conversation, projectId, orderId);
  const savedTurn = turn(row.turn);
  if (saved.id !== input.conversation_id || savedTurn.client_turn_id !== input.client_turn_id ||
      savedTurn.sequence !== input.expected_sequence + 1 || savedTurn.sequence > (saved.latest_sequence ?? 0)) {
    invalid('saved turn link');
  }
  return { conversation: saved, turn: savedTurn };
}

export async function getConversationTurnByClient(projectId: string, orderId: string, clientTurnId: string,
  signal?: AbortSignal): Promise<{ conversation_id: string; profile_id: string; turn: ConversationTurn }> {
  const row = object(await request(`${path(projectId, orderId)}/turns/by-client/${encodeURIComponent(clientTurnId)}`, { signal }),
    'saved submission');
  const savedTurn = turn(row.turn);
  if (savedTurn.client_turn_id !== clientTurnId) invalid('saved submission ID');
  return { conversation_id: string(row.conversation_id, 'saved conversation ID'),
    profile_id: string(row.profile_id, 'saved profile ID'), turn: savedTurn };
}
