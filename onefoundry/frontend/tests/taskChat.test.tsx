import { webcrypto } from 'node:crypto';
import { afterEach, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import type { Profile } from '../src/api';
import { getConversation } from '../src/conversationApi';
import { getTeamSession } from '../src/taskSessionApi';
import { TaskChatView } from '../src/TaskChatView';

vi.mock('../src/taskSessionApi', async (importOriginal) => ({
  ...await importOriginal<typeof import('../src/taskSessionApi')>(),
  getTeamSession: vi.fn().mockRejectedValue(new (await import('../src/api')).ApiError('No team session', 404)),
}));

const projectId = 'project-1';
const orderId = 'order-1';
const clientTurnId = '4a00f17d-0ca5-4a0d-9054-fefad75e9df6';
const profile: Profile = { id: 'profile-1', project_id: projectId, kind: 'custom', name: 'Researcher',
  reports_to_profile_id: 'chief-1', model: 'text-model', model_account_id: 'account-1',
  instructions: 'Research carefully', memory_scope: 'project', allowed_tools: [], autonomy_level: 'manual',
  configured: true, permissions_active: false };
const order = { id: orderId, project_id: projectId, title: 'Research launch', instructions: 'Summarize demand',
  profile_id: profile.id, status: 'ready', version: 1, block_reason: null, blocked_from_status: null,
  created_at: '2026-09-27T12:00:00Z' };
const path = `/api/projects/${projectId}/work-orders/${orderId}/conversation`;
const byClientPath = `${path}/turns/by-client/${clientTurnId}`;
const historyPath = `/api/projects/${projectId}/work-orders/${orderId}/conversations`;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

function conversation(turns: unknown[] = [], status: 'open' | 'busy' | 'unknown' = 'open') {
  return { id: 'conversation-1', project_id: projectId, work_order_id: orderId, profile_id: profile.id,
    status, turns };
}

function turn(status: 'done' | 'unknown' | 'failed' | 'running', assistantText: string | null) {
  return { id: 'turn-1', client_turn_id: clientTurnId, sequence: 1, status, operator_text: 'What did you find?',
    assistant_text: assistantText, assistant_actor_id: assistantText ? `profile:${profile.id}` : null,
    reason: status === 'unknown' ? 'provider_outcome_unknown' : null, provider: 'openai', model_id: 'text-model',
    input_tokens: status === 'done' ? 30 : null, output_tokens: status === 'done' ? 12 : null,
    cost_usd: null, progress_updates: [], created_at: '2026-09-27T12:01:00Z',
    finished_at: status === 'running' ? null : '2026-09-27T12:01:03Z' };
}

it('shows saved agent updates and a useful unknown-outcome explanation', async () => {
  const saved = conversation([{ ...turn('unknown', null),
    progress_updates: ['I will inspect the current assignment.'] }], 'unknown');
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url === `/api/projects/${projectId}/work-orders`) return json([order]);
    if (url === historyPath) return json([]);
    if (url === path) return json(saved);
    if (url === `/api/projects/${projectId}/runs`) return json([]);
    throw new Error(`Unexpected ${url}`);
  }));
  render(<TaskChatView projectId={projectId} profiles={[profile]}
    onOpenWorkOrders={() => {}} onOpenModels={() => {}} />);
  expect(await screen.findByText('I will inspect the current assignment.')).toBeInTheDocument();
  expect(screen.getByText(/Its outcome is unknown; continue the conversation/)).toBeInTheDocument();
});

afterEach(() => { vi.unstubAllGlobals(); window.sessionStorage.clear(); });

it('creates a second named chat under one work order and keeps the first transcript separate', async () => {
  const nextId = '29959a47-3501-4bc8-98b0-e0f894e62791';
  stubCrypto(() => nextId);
  const first = { ...conversation([turn('done', 'First chat answer.')]), title: 'Main chat', is_active: true };
  const second = { ...conversation(), id: nextId, title: 'Follow-up', is_active: true };
  let created = false;
  const summary = (saved: typeof first) => ({ ...saved, turns: undefined, created_at: order.created_at });
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === `/api/projects/${projectId}/work-orders`) return json([order]);
    if (url === path && !init?.method) return json(created ? second : first);
    if (url === historyPath && init?.method === 'POST') {
      expect(JSON.parse(String(init.body))).toEqual({ id: nextId, title: 'Follow-up' });
      created = true;
      return json(second, 201);
    }
    if (url === historyPath) return json(created ? [summary(second), summary({ ...first, is_active: false })] : [summary(first)]);
    if (url === `${historyPath}/${first.id}`) return json({ ...first, is_active: false });
    throw new Error(`Unexpected path ${url}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<TaskChatView projectId={projectId} profiles={[profile]} onOpenWorkOrders={() => {}} onOpenModels={() => {}} />);
  expect(await screen.findByText('First chat answer.')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '+ New chat' }));
  await user.type(screen.getByLabelText('New chat name'), 'Follow-up');
  await user.click(screen.getByRole('button', { name: 'Create chat' }));
  expect(await screen.findByRole('button', { name: 'Follow-up · Researcher' })).toHaveAttribute('aria-current', 'page');
  expect(screen.queryByText('First chat answer.')).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: /Main chat · Researcher/ }));
  expect(await screen.findByText('First chat answer.')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Send message' })).not.toBeInTheDocument();
});

it('rejects a malformed saved skill selection on a conversation turn', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(conversation([{
    ...turn('done', 'A reply.'), selected_skills: [{ skill_id: 'skill-1', version: 1,
      sha256: 'not-a-hash', selection_source: 'automatic' }],
  }]))));
  await expect(getConversation(projectId, orderId)).rejects.toThrow('invalid selected skill hash');
});

it('shows saved rule readback and opens Automation from a project chat', async () => {
  const saved = conversation([{ ...turn('done', 'The rule is ready.'), source_kind: 'discord',
    automation_decision: { decision: 'create_rule', status: 'saved', rule_id: 'rule-1',
      rule_version: 1, project_version: 2, plan_version: 3, error_code: null } }]);
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url === `/api/projects/${projectId}/work-orders`) return json([order]);
    if (url === historyPath) return json([]);
    if (url === path) return json(saved);
    if (url === `/api/projects/${projectId}/runs`) return json([]);
    throw new Error(`Unexpected ${url}`);
  }));
  const openAutomation = vi.fn();
  const user = userEvent.setup();
  render(<TaskChatView projectId={projectId} profiles={[profile]} onOpenWorkOrders={() => {}}
    onOpenModels={() => {}} onOpenAutomation={openAutomation} />);
  expect(await screen.findByText(/Saved Automation rule version 1/)).toBeInTheDocument();
  expect(screen.getByText(/Project goal version 2 saved too/)).toBeInTheDocument();
  expect(screen.getByText(/Project plan version 3 saved too/)).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Open Automation' }));
  expect(openAutomation).toHaveBeenCalledOnce();
});

it('shows failed action proposals while leaving ordinary answers uncluttered', async () => {
  let saved = conversation([{ ...turn('done', 'I propose a Researcher assignment.'),
    automation_decision: { decision: 'unavailable', status: 'unavailable', rule_id: null,
      rule_version: null, project_version: null, plan_version: null,
      assigned_work_order_id: null, error_code: 'invalid_action_proposal', assignments: [] } }]);
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url === `/api/projects/${projectId}/work-orders`) return json([order]);
    if (url === `/api/projects/${projectId}/runs`) return json([]);
    if (url === historyPath) return json([]);
    if (url === path) return json(saved);
    throw new Error(`Unexpected path ${url}`);
  }));
  const user = userEvent.setup();
  render(<TaskChatView projectId={projectId} profiles={[profile]} onOpenWorkOrders={() => {}} onOpenModels={() => {}} />);
  expect(await screen.findByText(/The proposed action was invalid/)).toBeInTheDocument();
  saved = conversation([{ ...turn('done', 'Here is the answer.'),
    automation_decision: { decision: 'unavailable', status: 'unavailable', rule_id: null,
      rule_version: null, project_version: null, plan_version: null,
      assigned_work_order_id: null, error_code: 'unstructured_reply', assignments: [] } }]);
  await user.click(screen.getByRole('button', { name: 'Refresh saved state' }));
  expect(await screen.findByText('Here is the answer.')).toBeInTheDocument();
  expect(screen.queryByText(/The proposed action was invalid/)).not.toBeInTheDocument();
});

it('shows the readable reply from a saved malformed proposal and keeps raw details expandable', async () => {
  const raw = JSON.stringify({ reply: 'I propose Researcher and QA.', decision: 'delegate_team', team: [] });
  const saved = conversation([{ ...turn('done', raw),
    automation_decision: { decision: 'unavailable', status: 'unavailable', rule_id: null,
      rule_version: null, project_version: null, plan_version: null,
      assigned_work_order_id: null, error_code: 'invalid_decision_format', assignments: [] } }]);
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url === `/api/projects/${projectId}/work-orders`) return json([order]);
    if (url === `/api/projects/${projectId}/runs`) return json([]);
    if (url === historyPath) return json([]);
    if (url === path) return json(saved);
    throw new Error(`Unexpected path ${url}`);
  }));
  render(<TaskChatView projectId={projectId} profiles={[profile]} onOpenWorkOrders={() => {}} onOpenModels={() => {}} />);
  expect(await screen.findByText('Action rejected')).toBeInTheDocument();
  expect(screen.getByText(raw)).toBeInTheDocument();
  expect(screen.getByText(/The proposed action was invalid/)).toBeInTheDocument();
});

it('opens the Chief assignment from its saved chat decision', async () => {
  const saved = conversation([{ ...turn('done', 'Draft assignment saved. The agent has not run yet.'),
    automation_decision: { decision: 'delegate_automation', status: 'saved', rule_id: null,
      rule_version: null, project_version: null, assigned_work_order_id: 'child-1', error_code: null } }]);
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url === `/api/projects/${projectId}/work-orders`) return json([order]);
    if (url === historyPath) return json([]);
    if (url === path) return json(saved);
    if (url === `/api/projects/${projectId}/runs`) return json([]);
    throw new Error(`Unexpected ${url}`);
  }));
  const onManageOrder = vi.fn();
  const user = userEvent.setup();
  render(<TaskChatView projectId={projectId} profiles={[profile]} onOpenWorkOrders={() => {}}
    onOpenModels={() => {}} onManageOrder={onManageOrder} />);
  expect(await screen.findByText(/Saved 1 agent assignment/)).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Open assignment' }));
  expect(onManageOrder).toHaveBeenCalledWith('child-1');
});

it('shows linked team assignments and labels automatic Chief follow-up honestly', async () => {
  const saved = conversation([{ ...turn('done', 'Two assignments saved.'), source_kind: 'system_followup',
    automation_decision: { decision: 'delegate_team', status: 'saved', rule_id: null,
      rule_version: null, project_version: null, plan_version: null,
      assigned_work_order_id: 'child-1', error_code: null,
      assignments: [
        { id: 'child-1', title: 'Research claims', status: 'ready', profile_name: 'Researcher' },
        { id: 'child-2', title: 'Review draft', status: 'draft', profile_name: 'QA' },
      ] } }]);
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url === `/api/projects/${projectId}/work-orders`) return json([order]);
    if (url === historyPath) return json([]);
    if (url === path) return json(saved);
    if (url === `/api/projects/${projectId}/runs`) return json([]);
    throw new Error(`Unexpected ${url}`);
  }));
  const onManageOrder = vi.fn();
  const user = userEvent.setup();
  render(<TaskChatView projectId={projectId} profiles={[profile]} onOpenWorkOrders={() => {}}
    onOpenModels={() => {}} onManageOrder={onManageOrder} />);
  expect(await screen.findByText('Project update')).toBeInTheDocument();
  expect(screen.getByText(/Saved 2 agent assignments/)).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'QA: Review draft · draft' }));
  expect(onManageOrder).toHaveBeenCalledWith('child-2');
  expect(screen.queryByRole('button', { name: 'Open assignment' })).not.toBeInTheDocument();
});

it('copies a saved reply and loads an earlier operator message into the composer without changing history', async () => {
  const saved = conversation([turn('done', 'Audience research is ready.')]);
  const copy = vi.fn().mockResolvedValue(undefined);
  const fetcher = vi.fn(async (url: string, _init?: RequestInit) => {
    void _init;
    if (url === `/api/projects/${projectId}/work-orders`) return json([order]);
    if (url === historyPath) return json([]);
    if (url === path) return json(saved);
    if (url === `/api/projects/${projectId}/runs`) return json([]);
    throw new Error(`Unexpected path ${url}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: copy } });
  render(<TaskChatView projectId={projectId} profiles={[profile]} onOpenWorkOrders={() => {}} onOpenModels={() => {}} />);
  expect(await screen.findByText('Audience research is ready.')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Copy agent message' }));
  expect(copy).toHaveBeenCalledWith('Audience research is ready.');
  const composer = screen.getByLabelText('Message to Researcher');
  await user.type(composer, 'Keep this unfinished draft');
  await user.click(screen.getByRole('button', { name: 'Edit agent message' }));
  expect(composer).toHaveValue('Audience research is ready.');
  await user.click(screen.getByRole('button', { name: 'Cancel edit' }));
  expect(composer).toHaveValue('Keep this unfinished draft');
  await user.click(screen.getByRole('button', { name: 'Edit your message' }));
  expect(composer).toHaveValue('What did you find?');
  expect(screen.getByText(/Send saves this as a new message/)).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Cancel edit' }));
  expect(composer).toHaveValue('Keep this unfinished draft');
  expect(within(screen.getByLabelText('Saved conversation turns')).getByText('What did you find?')).toBeInTheDocument();
  expect(fetcher.mock.calls.some(([url, init]) => url === `${path}/turns` && init?.method === 'POST')).toBe(false);
});

function stubCrypto(randomUUID: () => string) {
  vi.stubGlobal('crypto', { randomUUID, subtle: webcrypto.subtle });
}

it('opens a work-order conversation, sends one explicit turn, and displays saved readback', async () => {
  stubCrypto(() => clientTurnId);
  let opened = false;
  let saved = conversation();
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === `/api/projects/${projectId}/work-orders`) return json([order]);
    if (url === historyPath) return json([]);
    if (url === path && !init?.method) return opened ? json(saved) : json({ detail: 'conversation not opened' }, 404);
    if (url === historyPath) return json([]);
    if (url === path && init?.method === 'POST') { opened = true; return json(saved, 201); }
    if (url === `${path}/turns` && init?.method === 'POST') {
      expect(JSON.parse(String(init.body))).toEqual({ conversation_id: 'conversation-1', client_turn_id: clientTurnId, expected_sequence: 0,
        text: 'What did you find?', attachment_ids: [], discussion_only: true });
      const reply = { ...turn('done', 'Demand is strongest among small teams.'), selected_skills: [
        { skill_id: 'auto-skill', skill_name: 'Market scan', version: 3, sha256: 'a'.repeat(64), selection_source: 'automatic' },
        { skill_id: 'pinned-skill', version: 1, sha256: 'b'.repeat(64), selection_source: 'pinned' },
      ] };
      saved = conversation([reply]);
      return json({ conversation: saved, turn: reply }, 201);
    }
    throw new Error(`Unexpected path ${url}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<TaskChatView projectId={projectId} profiles={[profile]} initialOrderId={orderId}
    onOpenWorkOrders={() => {}} onOpenModels={() => {}} />);
  expect(await screen.findByText('Start a conversation for this task')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Open conversation' }));
  expect(await screen.findByText('Conversation ready')).toBeInTheDocument();
  await user.type(screen.getByLabelText('Message to Researcher'), 'What did you find?');
  await user.click(screen.getByRole('button', { name: 'Send message' }));
  expect(await screen.findByText('Demand is strongest among small teams.')).toBeInTheDocument();
  const transcript = screen.getByLabelText('Saved conversation turns');
  expect(within(transcript).getByText('What did you find?')).toBeInTheDocument();
  expect(within(transcript).getByText(/openai · text-model · 42 tokens · Cost unavailable/)).toBeInTheDocument();
  expect(within(transcript).getByText(/Skills: Automatic · Market scan v3; Pinned · Saved procedure v1/)).toBeInTheDocument();
  expect(screen.getByText('Reply saved.')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Refresh saved state' }));
  expect(await screen.findByText('Demand is strongest among small teams.')).toBeInTheDocument();
  expect(fetcher.mock.calls.filter(([url]) => url === `${path}/turns`)).toHaveLength(1);
  await user.click(screen.getByRole('button', { name: 'Assignment' }));
  expect(screen.getByText(/does not start coding agents or approve outbound actions/i)).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Team' }));
  expect(screen.getByText(/No team assignments are saved/)).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Refresh saved state' }));
  expect(screen.getByText('Team work')).toBeInTheDocument();
});

it('shows an outgoing message while the agent is working without offering a false retry', async () => {
  stubCrypto(() => clientTurnId);
  let finishSend: ((value: Response) => void) | undefined;
  const waiting = new Promise<Response>((resolve) => { finishSend = resolve; });
  let saved = conversation();
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === `/api/projects/${projectId}/work-orders`) return json([order]);
    if (url === `/api/projects/${projectId}/runs`) return json([]);
    if (url === historyPath) return json([]);
    if (url === path) return json(saved);
    if (url === byClientPath) return json({ conversation_id: saved.id, profile_id: profile.id,
      turn: saved.turns[0] });
    if (url === `${path}/turns` && init?.method === 'POST') return waiting;
    throw new Error(`Unexpected path ${url}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<TaskChatView projectId={projectId} profiles={[profile]} onOpenWorkOrders={() => {}} onOpenModels={() => {}} />);
  await screen.findByText('Conversation ready');
  await user.type(screen.getByLabelText('Message to Researcher'), 'What did you find?');
  await user.click(screen.getByRole('button', { name: 'Send message' }));
  await waitFor(() => expect(fetcher.mock.calls.some(([url]) => url === `${path}/turns`)).toBe(true));
  expect(within(screen.getByLabelText('Saved conversation turns')).getByText('What did you find?')).toBeInTheDocument();
  expect(screen.getByText('Saving request and connecting to the agent…')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Retry same submission' })).not.toBeInTheDocument();
  saved = conversation([{ ...turn('running', null), progress_updates: ['I will check the evidence.'] }], 'busy');
  expect(await screen.findByText('I will check the evidence.')).toBeInTheDocument();
  saved = conversation([turn('done', 'Research is saved.')]);
  await act(async () => { finishSend?.(json({ conversation: conversation([turn('done', 'Research is saved.')]),
    turn: turn('done', 'Research is saved.') }, 201)); });
  expect(await screen.findByText('Research is saved.')).toBeInTheDocument();
});

it('uploads a selected file before Send and shows the exact saved attachment', async () => {
  const fileId = '8c2dc987-cab2-40d1-b822-f39bd912af78';
  let nextId = 0;
  stubCrypto(() => nextId++ === 0 ? fileId : clientTurnId);
  let saved = conversation();
  let uploaded = false;
  const fileRow = { id: fileId, project_id: projectId, display_name: 'brief.md', extension: 'md',
    mime_type: 'text/markdown', sha256: 'a'.repeat(64), byte_size: 12, status: 'ready_text',
    reason_code: null, extractor_version: 'input-v1' };
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === `/api/projects/${projectId}/work-orders`) return json([order]);
    if (url === historyPath) return json([]);
    if (url === path && !init?.method) return json(saved);
    if (url === `/api/projects/${projectId}/input-files?work_order_id=${orderId}`) return json(uploaded ? [fileRow] : []);
    if (url === `/api/projects/${projectId}/input-files/${fileId}`) return uploaded ? json(fileRow) : json({ detail: 'missing' }, 404);
    if (url.startsWith(`/api/projects/${projectId}/input-files?scope=staged`) && init?.method === 'POST') {
      uploaded = true;
      return json(fileRow, 201);
    }
    if (url === `${path}/turns` && init?.method === 'POST') {
      expect(uploaded).toBe(true);
      expect(JSON.parse(String(init.body)).attachment_ids).toEqual([fileId]);
      const reply = { ...turn('done', 'Plan reviewed.'), attachments: [
        { id: fileId, display_name: 'brief.md', sha256: 'a'.repeat(64) }], used_sections: [
        { file_id: fileId, file_name: 'brief.md', file_sha256: 'a'.repeat(64),
          source_label: 'brief.md: text 1', chunk_sha256: 'b'.repeat(64), byte_count: 12 }],
        input_sections_omitted: 0 };
      saved = conversation([reply]);
      return json({ conversation: saved, turn: reply }, 201);
    }
    throw new Error(`Unexpected path ${url}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<TaskChatView projectId={projectId} profiles={[profile]} onOpenWorkOrders={() => {}} onOpenModels={() => {}} />);
  await screen.findByText('Conversation ready');
  await user.click(screen.getByRole('button', { name: 'Attach files' }));
  fireEvent.change(screen.getByLabelText('Attach files to this message'), {
    target: { files: [new File(['Brief body!!'], 'brief.md', { type: 'text/markdown' })] },
  });
  await user.type(screen.getByLabelText('Message to Researcher'), 'What did you find?');
  await user.click(screen.getByRole('button', { name: 'Send message' }));
  expect(await screen.findByText('Plan reviewed.')).toBeInTheDocument();
  expect(screen.getByText('Attached: brief.md')).toBeInTheDocument();
  expect(screen.getByText(/File inputs used: Section: brief.md: text 1/)).toBeInTheDocument();
});

it('allows an explicit text turn on a completed work order without changing its status', async () => {
  stubCrypto(() => clientTurnId);
  const completed = { ...order, status: 'done' };
  const saved = conversation();
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === `/api/projects/${projectId}/work-orders`) return json([completed]);
    if (url === historyPath) return json([]);
    if (url === path && !init?.method) return json(saved);
    if (url === `${path}/turns` && init?.method === 'POST') {
      const reply = { ...turn('done', 'Here is the follow-up.'), cost_usd: 0.00123 };
      return json({ conversation: conversation([reply]), turn: reply }, 201);
    }
    throw new Error(`Unexpected path ${url}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<TaskChatView projectId={projectId} profiles={[profile]} onOpenWorkOrders={() => {}} onOpenModels={() => {}} />);
  await screen.findByText('Conversation ready');
  await user.type(screen.getByLabelText('Message to Researcher'), 'What did you find?');
  await user.click(screen.getByRole('button', { name: 'Send message' }));
  expect(await screen.findByText('Here is the follow-up.')).toBeInTheDocument();
  expect(screen.getByText(/openai · text-model · 42 tokens · Cost \$0.00123/)).toBeInTheDocument();
  expect(screen.getByText('Completed')).toBeInTheDocument();
  expect(fetcher.mock.calls.filter(([url]) => url === `${path}/turns`)).toHaveLength(1);
});

it('reads back an unknown provider outcome and blocks another send', async () => {
  stubCrypto(() => clientTurnId);
  let saved = conversation();
  const unknownTurn = { ...turn('unknown', null), diagnostic_code: 'stream_ended_without_completion' };
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === `/api/projects/${projectId}/work-orders`) return json([order]);
    if (url === historyPath) return json([]);
    if (url === path && !init?.method) return json(saved);
    if (url === byClientPath) return json({ conversation_id: 'conversation-1', profile_id: profile.id, turn: unknownTurn });
    if (url === `${path}/turns` && init?.method === 'POST') {
      saved = conversation([unknownTurn], 'unknown');
      return json({ detail: 'agent response outcome unknown', turn_id: 'turn-1' }, 503);
    }
    throw new Error(`Unexpected path ${url}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<TaskChatView projectId={projectId} profiles={[profile]} onOpenWorkOrders={() => {}} onOpenModels={() => {}} />);
  await screen.findByText('Conversation ready');
  await user.type(screen.getByLabelText('Message to Researcher'), 'What did you find?');
  await user.click(screen.getByRole('button', { name: 'Send message' }));
  expect(await screen.findByText(/provider outcome is unknown.*do not resend/i)).toBeInTheDocument();
  expect(screen.getByText('Outcome unknown')).toBeInTheDocument();
  expect(screen.getByText('Response issue: stream ended without completion')).toBeInTheDocument();
  expect(screen.getByText('openai · text-model')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Send message' })).toBeDisabled();
  await waitFor(() => expect(fetcher.mock.calls.filter(([url]) => url === `${path}/turns`)).toHaveLength(1));
});

it('continues the same conversation after acknowledging an unknown turn without replaying it', async () => {
  const nextClientTurnId = '777f21fd-6e1d-4d3e-b998-4b33edcaa81f';
  let generated = 0;
  stubCrypto(() => generated++ === 0 ? clientTurnId : nextClientTurnId);
  const unknownTurn = turn('unknown', null);
  const nextTurn = { ...turn('done', 'Here is the next answer.'), id: 'turn-2', client_turn_id: nextClientTurnId,
    sequence: 2, operator_text: 'What should we do next?' };
  let saved = conversation();
  let sends = 0;
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === `/api/projects/${projectId}/work-orders`) return json([order]);
    if (url === historyPath) return json([]);
    if (url === path && !init?.method) return json(saved);
    if (url === byClientPath) return json({ conversation_id: 'conversation-1', profile_id: profile.id, turn: unknownTurn });
    if (url === `${path}/turns/turn-1/acknowledge-unknown` && init?.method === 'POST') {
      saved = conversation([unknownTurn], 'open');
      return json(saved);
    }
    if (url === `${path}/turns` && init?.method === 'POST') {
      sends += 1;
      if (sends === 1) {
        saved = conversation([unknownTurn], 'unknown');
        return json({ detail: 'agent response outcome unknown', turn_id: 'turn-1' }, 503);
      }
      expect(JSON.parse(String(init.body))).toEqual({ conversation_id: 'conversation-1',
        client_turn_id: nextClientTurnId, expected_sequence: 1, text: 'What should we do next?', attachment_ids: [], discussion_only: true });
      saved = conversation([unknownTurn, nextTurn]);
      return json({ conversation: saved, turn: nextTurn }, 201);
    }
    throw new Error(`Unexpected path ${url}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<TaskChatView projectId={projectId} profiles={[profile]} onOpenWorkOrders={() => {}} onOpenModels={() => {}} />);
  await screen.findByText('Conversation ready');
  await user.type(screen.getByLabelText('Message to Researcher'), 'What did you find?');
  await user.click(screen.getByRole('button', { name: 'Send message' }));
  expect(await screen.findByRole('button', { name: 'Continue conversation' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Send message' })).toBeDisabled();
  await user.click(screen.getByRole('button', { name: 'Continue conversation' }));
  expect(await screen.findByText(/earlier outcome remains unknown/i)).toBeInTheDocument();
  expect(window.sessionStorage.getItem(`onefoundry:task-chat-pending:${projectId}:${orderId}`)).toBeNull();
  await user.type(screen.getByLabelText('Message to Researcher'), 'What should we do next?');
  await user.click(screen.getByRole('button', { name: 'Send message' }));
  expect(await screen.findByText('Here is the next answer.')).toBeInTheDocument();
  expect(screen.getByText('Outcome unknown')).toBeInTheDocument();
  expect(sends).toBe(2);
});

it('keeps an unconfirmed send across refresh and remount, then retries only the same submission ID', async () => {
  const uuid = vi.fn(() => clientTurnId);
  stubCrypto(uuid);
  let saved = conversation();
  let sends = 0;
  const bodies: unknown[] = [];
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === `/api/projects/${projectId}/work-orders`) return json([order]);
    if (url === historyPath) return json([]);
    if (url === path && !init?.method) return json(saved);
    if (url === byClientPath) return json({ detail: 'not found' }, 404);
    if (url === `${path}/turns` && init?.method === 'POST') {
      bodies.push(JSON.parse(String(init.body)));
      sends += 1;
      if (sends === 1) return json({ detail: 'agent response outcome unknown', turn_id: 'turn-1' }, 503);
      const reply = turn('done', 'Recovered saved reply.');
      saved = conversation([reply]);
      return json({ conversation: saved, turn: reply }, 201);
    }
    throw new Error(`Unexpected path ${url}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  const mounted = render(<TaskChatView projectId={projectId} profiles={[profile]} onOpenWorkOrders={() => {}} onOpenModels={() => {}} />);
  await screen.findByText('Conversation ready');
  await user.type(screen.getByLabelText('Message to Researcher'), 'What did you find?');
  await user.click(screen.getByRole('button', { name: 'Send message' }));
  expect(await screen.findByText(/did not confirm a terminal turn/i)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Send message' })).toBeDisabled();
  await user.click(screen.getByRole('button', { name: 'Refresh saved state' }));
  expect(await screen.findByRole('button', { name: 'Retry same submission' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Send message' })).toBeDisabled();
  expect(sends).toBe(1);
  const stored = window.sessionStorage.getItem(`onefoundry:task-chat-pending:${projectId}:${orderId}`);
  expect(stored).toContain(clientTurnId);
  expect(stored).toContain('textHash');
  expect(stored).not.toContain('What did you find?');

  mounted.unmount();
  render(<TaskChatView projectId={projectId} profiles={[profile]} onOpenWorkOrders={() => {}} onOpenModels={() => {}} />);
  await screen.findByRole('button', { name: 'Retry same submission' });
  expect(screen.getByRole('button', { name: 'Send message' })).toBeDisabled();
  await user.click(screen.getByRole('button', { name: 'Retry same submission' }));
  expect(await screen.findByText(/Re-enter the exact original message/)).toBeInTheDocument();
  await user.type(screen.getByLabelText('Re-enter original message'), 'Wrong message');
  await user.click(screen.getByRole('button', { name: 'Retry same submission' }));
  expect(await screen.findByText(/does not match the original submission/)).toBeInTheDocument();
  expect(sends).toBe(1);
  await user.clear(screen.getByLabelText('Re-enter original message'));
  await user.type(screen.getByLabelText('Re-enter original message'), 'What did you find?');
  await user.click(screen.getByRole('button', { name: 'Retry same submission' }));
  expect(await screen.findByText('Recovered saved reply.')).toBeInTheDocument();
  expect(bodies).toEqual([
    { conversation_id: 'conversation-1', client_turn_id: clientTurnId, expected_sequence: 0, text: 'What did you find?', attachment_ids: [], discussion_only: true },
    { conversation_id: 'conversation-1', client_turn_id: clientTurnId, expected_sequence: 0, text: 'What did you find?', attachment_ids: [], discussion_only: true },
  ]);
  expect(uuid).toHaveBeenCalledTimes(1);
  expect(window.sessionStorage.getItem(`onefoundry:task-chat-pending:${projectId}:${orderId}`)).toBeNull();
});

it('keeps former role transcripts read-only and reconciles an old submission after handoff', async () => {
  stubCrypto(() => clientTurnId);
  const nextProfile = { ...profile, id: 'profile-2', name: 'GTM' };
  const nextConversation = { ...conversation(), id: 'conversation-2', profile_id: nextProfile.id };
  let handedOff = false;
  let oldOutcome: 'unknown' | 'done' = 'unknown';
  const oldConversation = () => conversation([turn(oldOutcome, oldOutcome === 'done' ? 'Old role reply.' : null)],
    oldOutcome === 'unknown' ? 'unknown' : 'open');
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === `/api/projects/${projectId}/work-orders`) return json([{ ...order, profile_id: handedOff ? nextProfile.id : profile.id }]);
    if (url === historyPath) return json(handedOff ? [
      { id: 'conversation-1', project_id: projectId, work_order_id: orderId, profile_id: profile.id,
        status: oldOutcome === 'unknown' ? 'unknown' : 'open', created_at: '2026-09-27T12:00:00Z' },
      { id: 'conversation-2', project_id: projectId, work_order_id: orderId, profile_id: nextProfile.id,
        status: 'open', created_at: '2026-09-27T12:05:00Z' },
    ] : []);
    if (url === path && !init?.method) return json(handedOff ? nextConversation : conversation());
    if (url === `${historyPath}/conversation-1`) return json(oldConversation());
    if (url === byClientPath) return handedOff
      ? json({ conversation_id: 'conversation-1', profile_id: profile.id, turn: turn(oldOutcome, oldOutcome === 'done' ? 'Old role reply.' : null) })
      : json({ detail: 'not found' }, 404);
    if (url === `${path}/turns` && init?.method === 'POST') {
      expect(handedOff).toBe(false);
      expect(JSON.parse(String(init.body)).conversation_id).toBe('conversation-1');
      return json({ detail: 'outcome unknown' }, 503);
    }
    throw new Error(`Unexpected path ${url}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  const first = render(<TaskChatView projectId={projectId} profiles={[profile, nextProfile]} onOpenWorkOrders={() => {}} onOpenModels={() => {}} />);
  await screen.findByText('Conversation ready');
  await user.type(screen.getByLabelText('Message to Researcher'), 'What did you find?');
  await user.click(screen.getByRole('button', { name: 'Send message' }));
  expect(await screen.findByText(/did not confirm a terminal turn/i)).toBeInTheDocument();
  first.unmount();

  handedOff = true;
  render(<TaskChatView projectId={projectId} profiles={[profile, nextProfile]} onOpenWorkOrders={() => {}} onOpenModels={() => {}} />);
  const former = await screen.findByRole('button', { name: /Researcher · Outcome uncertain/ });
  expect(screen.getByRole('button', { name: 'Send message' })).toBeDisabled();
  expect(screen.queryByRole('button', { name: 'Retry same submission' })).not.toBeInTheDocument();
  await user.click(former);
  expect(await screen.findByText(/Main chat with Researcher · Outcome uncertain/)).toBeInTheDocument();
  expect(screen.getByText('Outcome unknown')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Send message' })).not.toBeInTheDocument();

  oldOutcome = 'done';
  await user.click(screen.getByRole('button', { name: 'Refresh saved state' }));
  expect(await screen.findByText('Old role reply.')).toBeInTheDocument();
  await waitFor(() => expect(window.sessionStorage.getItem(`onefoundry:task-chat-pending:${projectId}:${orderId}`)).toBeNull());
  expect(screen.queryByRole('button', { name: 'Send message' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Main chat · GTM' }));
  expect(await screen.findByLabelText('Message to GTM')).toBeEnabled();
  expect(screen.getByRole('button', { name: 'Send message' })).toBeDisabled();
  expect(fetcher.mock.calls.filter(([url]) => url === `${path}/turns`)).toHaveLength(1);
});

it('rejects a secret-like draft before writing browser state or sending to the provider', async () => {
  stubCrypto(() => clientTurnId);
  const fetcher = vi.fn(async (url: string) => {
    if (url === `/api/projects/${projectId}/work-orders`) return json([order]);
    if (url === historyPath) return json([]);
    if (url === path) return json(conversation());
    throw new Error(`Unexpected path ${url}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<TaskChatView projectId={projectId} profiles={[profile]} onOpenWorkOrders={() => {}} onOpenModels={() => {}} />);
  await screen.findByText('Conversation ready');
  await user.type(screen.getByLabelText('Message to Researcher'), 'password: example-secret-value');
  await user.click(screen.getByRole('button', { name: 'Send message' }));
  expect(await screen.findByText(/appears to contain a secret/)).toBeInTheDocument();
  expect(window.sessionStorage.length).toBe(0);
  expect(fetcher.mock.calls.filter(([url]) => url === `${path}/turns`)).toHaveLength(0);
});

it('allows only one submission ID for rapid repeated Send events', async () => {
  const uuid = vi.fn(() => clientTurnId);
  stubCrypto(uuid);
  let release!: (response: Response) => void;
  const waiting = new Promise<Response>((resolve) => { release = resolve; });
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === `/api/projects/${projectId}/work-orders`) return json([order]);
    if (url === historyPath) return json([]);
    if (url === path && !init?.method) return json(conversation());
    if (url === `${path}/turns` && init?.method === 'POST') return waiting;
    throw new Error(`Unexpected path ${url}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<TaskChatView projectId={projectId} profiles={[profile]} onOpenWorkOrders={() => {}} onOpenModels={() => {}} />);
  await screen.findByText('Conversation ready');
  await user.type(screen.getByLabelText('Message to Researcher'), 'What did you find?');
  const form = screen.getByRole('button', { name: 'Send message' }).closest('form');
  expect(form).not.toBeNull();
  act(() => { fireEvent.submit(form!); fireEvent.submit(form!); });
  await waitFor(() => expect(fetcher.mock.calls.filter(([url]) => url === `${path}/turns`)).toHaveLength(1));
  expect(uuid).toHaveBeenCalledTimes(1);
  const reply = turn('done', 'One answer.');
  release(json({ conversation: conversation([reply]), turn: reply }, 201));
  expect(await screen.findByText('One answer.')).toBeInTheDocument();
});

it('requires a complete text binding and rejects malformed conversation ownership', async () => {
  const noModel = { ...profile, model: null, model_account_id: null };
  const onOpenModels = vi.fn();
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url === `/api/projects/${projectId}/work-orders`) return json([order]);
    if (url === historyPath) return json([]);
    if (url === path) return json(conversation());
    throw new Error(`Unexpected path ${url}`);
  }));
  const user = userEvent.setup();
  render(<TaskChatView projectId={projectId} profiles={[noModel]} onOpenWorkOrders={() => {}} onOpenModels={onOpenModels} />);
  await screen.findByText('Conversation ready');
  expect(screen.getByRole('button', { name: 'Send message' })).toBeDisabled();
  expect(screen.getByText(/needs instructions and a connected, discovered text model/i)).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Open AI connections' }));
  expect(onOpenModels).toHaveBeenCalledOnce();

  vi.stubGlobal('fetch', vi.fn(async () => json({ ...conversation(), project_id: 'different-project' })));
  await expect(getConversation(projectId, orderId)).rejects.toThrow('invalid conversation owner');
});


it('routes team results to artifact owners without requesting a legacy text output', async () => {
  vi.mocked(getTeamSession).mockResolvedValue({
    session: { id: 'team-main', profile_id: profile.id, status: 'closed', execution_mode: 'team', work_order_status: 'done' },
    messages: [], attempts: [], children: [], assignments: [], decision_causes: [], tool_calls: [],
  });
  const fetcher = vi.fn(async (url: string) => {
    if (url === `/api/projects/${projectId}/work-orders`) return json([{ ...order, status: 'done' }]);
    if (url === `/api/projects/${projectId}/runs`) return json([{ id: 'team-attempt', project_id: projectId,
      work_order_id: orderId, status: 'done', mode: 'live_text', profile_id: profile.id,
      started_at: order.created_at, finished_at: order.created_at, work_order_title: order.title,
      work_order_instructions: order.instructions, profile_model: 'fixture-text', tokens_used: 0,
      cost_usd: null, outcome_reason: 'team_turn_finished', triggered_by: null }]);
    if (url.endsWith('/team-session/artifacts')) return json([]);
    return json([]);
  });
  vi.stubGlobal('fetch', fetcher);
  render(<TaskChatView projectId={projectId} profiles={[profile]}
    onOpenWorkOrders={() => {}} onOpenModels={() => {}} />);
  const user = userEvent.setup();
  expect(await screen.findByText('Reopen completed work', { selector: 'summary' })).toBeInTheDocument();
  expect(screen.getByText('Reopen completed work', { selector: 'summary' }).closest('details')).not.toHaveAttribute('open');
  await user.click(await screen.findByRole('button', { name: 'Results' }));
  expect(await screen.findByText('No team artifact is saved yet. A plan or successful chat turn is not a deliverable.')).toBeInTheDocument();
  expect(fetcher.mock.calls.some(([url]) => url.includes('/live-text'))).toBe(false);
  vi.mocked(getTeamSession).mockRejectedValue(new (await import('../src/api')).ApiError('No team session', 404));
});

it('resumes a saved legacy submission after turn 100 and loads bounded older history', async () => {
  vi.stubGlobal('crypto', { subtle: webcrypto.subtle, randomUUID: () => clientTurnId });
  window.sessionStorage.setItem(`onefoundry:task-chat-pending:${projectId}:${orderId}`, JSON.stringify({
    projectId, orderId, conversationId: 'conversation-1', profileId: profile.id, clientTurnId,
    expectedSequence: 100, textHash: 'cdd955f91033f085b41d4d7897a5d4a283a051cc7e2bf9e2eea2d0305b6b6a0d', attachmentIds: [],
  }));
  // Use the actual exact-text digest, as pending recovery must never submit different text.
  const text = 'Continue the saved goal.';
  const digest = await webcrypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  const key = `onefoundry:task-chat-pending:${projectId}:${orderId}`;
  const pending = JSON.parse(window.sessionStorage.getItem(key) ?? '{}');
  pending.textHash = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
  window.sessionStorage.setItem(key, JSON.stringify(pending));
  let saved = { ...conversation([{ ...turn('done', 'Last confirmed reply.'), id: 'turn-100',
    client_turn_id: 'old-100', sequence: 100 }]), turns_omitted: 99, latest_sequence: 100 };
  let sent = false;
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === `/api/projects/${projectId}/work-orders`) return json([order]);
    if (url === `/api/projects/${projectId}/runs`) return json([]);
    if (url === historyPath) return json([]);
    if (url === path) return json(saved);
    if (url === byClientPath) return sent ? json({ conversation_id: saved.id, profile_id: profile.id, turn: saved.turns[0] })
      : json({detail: 'turn not found'}, 404);
    if (url === `${path}/turns` && init?.method === 'POST') {
      const request = JSON.parse(String(init.body));
      expect(request.client_turn_id).toBe(clientTurnId);
      expect(request.expected_sequence).toBe(100);
      sent = true;
      saved = { ...saved, turns: [{ ...turn('done', 'Reply after turn 100.'), sequence: 101 }],
        turns_omitted: 100, latest_sequence: 101 };
      return json({ conversation: saved, turn: saved.turns[0] }, 201);
    }
    if (url === `${historyPath}/conversation-1?before_sequence=101`) return json({
      ...saved, turns: [{ ...turn('done', 'Earlier saved reply.'), id: 'turn-100', client_turn_id: 'old-100', sequence: 100 }], turns_omitted: 99 });
    throw new Error(`Unexpected ${url}`);
  });
  vi.stubGlobal('fetch', fetch);
  const user = userEvent.setup();
  render(<TaskChatView projectId={projectId} profiles={[profile]} onOpenWorkOrders={() => {}} onOpenModels={() => {}} />);
  await screen.findByText('Last confirmed reply.');
  await user.type(screen.getByLabelText('Re-enter original message'), text);
  await user.click(screen.getByRole('button', {name: 'Retry same submission'}));
  await screen.findByText('Reply after turn 100.');
  expect(screen.getByRole('button', {name: '+ New chat'})).toBeEnabled();
  expect(window.sessionStorage.getItem(key)).toBeNull();
  await user.click(screen.getByRole('button', {name: 'Load earlier turns'}));
  await screen.findByText('Earlier saved reply.');
  expect(screen.getByText(/99 earlier turns remain saved/)).toBeInTheDocument();
  expect(fetch.mock.calls.filter(([url]) => url === `${path}/turns`)).toHaveLength(1);
});

it('refreshes stale work-order status and version when saved team readiness reaches review', async () => {
  vi.mocked(getTeamSession).mockResolvedValue({
    session: {id:'main',profile_id:profile.id,status:'open',execution_mode:'team'},
    messages:[],attempts:[],children:[],assignments:[],decision_causes:[],tool_calls:[],
  });
  let readinessRead = false;
  const completed = {...order,status:'needs_review',version:4,deliverable_kind:'operator_review',acceptance_checks:[]};
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url === `/api/projects/${projectId}/work-orders`) return json([readinessRead ? completed : order]);
    if (url === `/api/projects/${projectId}/runs`) return json([]);
    if (url === historyPath) return json([]);
    if (url === path) return json(conversation([]));
    if (url === `/api/projects/${projectId}/work-orders/${orderId}/team-session/readiness`) {
      readinessRead = true;
      return json({work_order_id:orderId,status:'needs_review',execution_mode:'team',worker_enabled:true,
        next_step:'Review saved results.',blockers:[],pending_approvals:0,queued_attempts:0,running_attempts:0,
        confirmed_results:12,ready_to_activate:false});
    }
    throw new Error(`Unexpected ${url}`);
  }));
  render(<TaskChatView projectId={projectId} profiles={[profile]} onOpenWorkOrders={() => {}} onOpenModels={() => {}} />);
  expect(await screen.findByText('Needs review')).toBeInTheDocument();
  expect(await screen.findByText('Operator acceptance')).toBeInTheDocument();
  expect(screen.queryByText('Ready to start')).not.toBeInTheDocument();
});

it('resizes and expands the mounted chat, traps keyboard focus, and restores draft and size on Escape', async () => {
  vi.mocked((await import('../src/taskSessionApi')).getTeamSession).mockRejectedValue(new (await import('../src/api')).ApiError('No team session', 404));
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url === `/api/projects/${projectId}/work-orders`) return json([order]);
    if (url === historyPath) return json([]);
    if (url === path) return json(conversation([turn('done', 'A saved reply.')]));
    if (url === `/api/projects/${projectId}/runs`) return json([]);
    throw new Error(`Unexpected ${url}`);
  }));
  const onBack = vi.fn(); const user = userEvent.setup();
  render(<div><button>Outside chat</button><TaskChatView projectId={projectId} profiles={[profile]}
    onOpenWorkOrders={onBack} onOpenModels={() => {}} /></div>);
  await screen.findByText('A saved reply.');
  const draft = screen.getByLabelText('Message to Researcher');
  await user.type(draft, 'Unsent draft remains here.');
  await user.click(screen.getByText('Resize chat'));
  fireEvent.change(screen.getByRole('slider', { name: /Chat width/ }), { target: { value: '80' } });
  fireEvent.change(screen.getByRole('slider', { name: /Chat height/ }), { target: { value: '85' } });
  const frame = screen.getByRole('region', { name: 'Task' });
  expect(frame.style.getPropertyValue('--task-chat-width')).toBe('80%');
  const expand = screen.getByRole('button', { name: 'Expand chat' });
  await user.click(expand);
  expect(screen.getByRole('dialog', { name: 'Research launch' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Exit full screen' })).toHaveFocus();
  expect(screen.getByRole('button', { name: 'Outside chat', hidden: true }).inert).toBe(true);
  await user.keyboard('{Escape}');
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(expand).toHaveFocus();
  expect(draft).toHaveValue('Unsent draft remains here.');
  expect(frame.style.getPropertyValue('--task-chat-height')).toBe('85%');
  expect(screen.getByRole('button', { name: 'Outside chat' }).inert).toBeFalsy();
  await user.click(expand);
  await user.click(screen.getByRole('button', { name: '← All work orders' }));
  expect(onBack).toHaveBeenCalledOnce();
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});
