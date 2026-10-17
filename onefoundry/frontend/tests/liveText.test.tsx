import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import type { Profile } from '../src/api';
import { RunTimelineView } from '../src/RunTimelineView';
import { WorkOrdersView } from '../src/WorkOrdersView';
import { getLiveTextOutput, runLiveText } from '../src/workApi';

const projectId = 'project-1';
const time = '2026-09-26T12:00:00Z';
const profile: Profile = { id: 'profile-1', project_id: projectId, kind: 'custom', name: 'Writer', reports_to_profile_id: null, model: 'text-a',
  model_account_id: 'account-1', instructions: 'Write clearly', memory_scope: 'project', allowed_tools: [],
  autonomy_level: 'manual', configured: true, permissions_active: false };
const order = { id: 'work-1', project_id: projectId, profile_id: profile.id, title: 'Write a note',
  instructions: 'Summarize the plan', status: 'ready', version: 1, block_reason: null,
  blocked_from_status: null, created_at: time };
const run = { id: 'run-1', project_id: projectId, work_order_id: order.id, work_order_title: order.title,
  work_order_instructions: order.instructions, profile_id: profile.id, profile_model: 'text-a', status: 'done',
  mode: 'live_text', tokens_used: 27, cost_usd: null, outcome_reason: '', started_at: time, finished_at: time };
const output = { id: 'output-1', project_id: projectId, run_id: run.id, profile_id: profile.id, actor_id: 'agent-1',
  account_id: 'account-1', provider: 'openai', model_id: 'text-a', text: 'A concise plan.', text_sha256: 'hash',
  input_tokens: 20, output_tokens: 7, cost_usd: null, cost_usd_ticks: null, provider_response_id: 'response-1',
  source_memory_ids: [], source_memories: [], created_at: time };
const modelAccount = { id: 'account-1', project_id: projectId, provider: 'openai', label: 'API', auth_kind: 'api_key',
  endpoint: null, credential_present: true, catalog_valid: true, model_count: 1, last_refreshed_at: time };
const catalog = [{ id: 'catalog-1', account_id: 'account-1', provider: 'openai', model_id: 'text-a',
  display_name: 'Text A', category: 'text', available: true }];
const providers = [{ id: 'openai', name: 'OpenAI', auth_kind: 'api_key', catalog_supported: true, text_runtime_available: true, connectable: true }];

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

afterEach(() => vi.unstubAllGlobals());

it('starts a bound live text run, verifies saved output, then opens its timeline', async () => {
  let started = false;
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/model/providers')) return json(providers);
    if (path.endsWith('/model/accounts')) return json([modelAccount]);
    if (path.endsWith('/model/catalog')) return json(catalog);
    if (path.endsWith('/work-orders/work-1/live-text-run') && init?.method === 'POST') {
      started = true;
      expect(init.body).toBe('{}');
      return json({ run, output }, 201);
    }
    if (path.endsWith('/work-orders')) return json([{ ...order, status: started ? 'done' : 'ready' }]);
    if (path.endsWith('/runs')) return json(started ? [run] : []);
    if (path.endsWith('/runs/run-1/live-text-output')) return json(output);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const onOpenRun = vi.fn();
  const user = userEvent.setup();
  render(<WorkOrdersView projectId={projectId} profiles={[profile]} onOpenRun={onOpenRun} />);
  await user.click(await screen.findByRole('button', { name: 'Run live text with provider' }));
  await waitFor(() => expect(onOpenRun).toHaveBeenCalledWith('run-1'));
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/live-text-run'))).toHaveLength(1);
});

it('blocks a subscription text run when its profile requires role tools', async () => {
  const subscribed = { ...modelAccount, provider: 'openai_codex_runtime', auth_kind: 'account_runtime' };
  const toolProfile = { ...profile, allowed_tools: ['create_draft_work_order_v1'] };
  const fetcher = vi.fn(async (path: string) => {
    if (path.endsWith('/model/providers')) return json([{ ...providers[0], id: 'openai_codex_runtime' }]);
    if (path.endsWith('/model/accounts')) return json([subscribed]);
    if (path.endsWith('/model/catalog')) return json([{ ...catalog[0], provider: 'openai_codex_runtime' }]);
    if (path.endsWith('/work-orders')) return json([order]);
    if (path.endsWith('/runs')) return json([]);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  render(<WorkOrdersView projectId={projectId} profiles={[toolProfile]} onOpenRun={vi.fn()} />);
  expect(await screen.findByText(/This model connection cannot execute OneFoundry role tools/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Run live text with provider' })).not.toBeInTheDocument();
  expect(fetcher.mock.calls.some(([path]) => path.endsWith('/live-text-run'))).toBe(false);
});

it('explains tool unavailability on an unknown subscription attempt without a misleading model prompt', async () => {
  const subscribed = { ...modelAccount, provider: 'openai_codex_runtime', auth_kind: 'account_runtime' };
  const toolProfile = { ...profile, allowed_tools: ['create_draft_work_order_v1'] };
  const unknownRun = { ...run, status: 'unknown', outcome_reason: 'provider_outcome_unknown' };
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.endsWith('/model/providers')) return json([{ ...providers[0], id: 'openai_codex_runtime' }]);
    if (path.endsWith('/model/accounts')) return json([subscribed]);
    if (path.endsWith('/model/catalog')) return json([{ ...catalog[0], provider: 'openai_codex_runtime' }]);
    if (path.endsWith('/work-orders')) return json([{ ...order, status: 'unknown' }]);
    if (path.endsWith('/runs')) return json([unknownRun]);
    throw new Error(`Unexpected path ${path}`);
  }));
  render(<WorkOrdersView projectId={projectId} profiles={[toolProfile]} onOpenRun={vi.fn()} />);
  expect(await screen.findByText(/This model connection cannot execute OneFoundry role tools/)).toBeInTheDocument();
  expect(screen.queryByText(/Connect a usable text model in Models and Team/)).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Restart as new live-text attempt' })).toBeDisabled();
});

it('reads back an unknown run after 503 and does not retry provider dispatch', async () => {
  let started = false;
  const unknownRun = { ...run, status: 'unknown', tokens_used: null, outcome_reason: 'Provider outcome unknown' };
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/model/providers')) return json(providers);
    if (path.endsWith('/model/accounts')) return json([modelAccount]);
    if (path.endsWith('/model/catalog')) return json(catalog);
    if (path.endsWith('/work-orders/work-1/live-text-run') && init?.method === 'POST') {
      started = true;
      return json({ detail: 'live inference outcome unknown', run_id: 'run-1' }, 503);
    }
    if (path.endsWith('/work-orders')) return json([{ ...order, status: started ? 'unknown' : 'ready' }]);
    if (path.endsWith('/runs/run-1')) return json({ ...unknownRun, events: [], profile_snapshots: [] });
    if (path.endsWith('/runs')) return json(started ? [unknownRun] : []);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const onOpenRun = vi.fn();
  const user = userEvent.setup();
  render(<WorkOrdersView projectId={projectId} profiles={[profile]} onOpenRun={onOpenRun} />);
  await user.click(await screen.findByRole('button', { name: 'Run live text with provider' }));
  await waitFor(() => expect(onOpenRun).toHaveBeenCalledWith('run-1'));
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/live-text-run'))).toHaveLength(1);
});

it('edits an unknown work order and starts a distinct attempt only after acknowledgment', async () => {
  let savedOrder = { ...order, status: 'unknown', version: 4 };
  const oldRun = { ...run, status: 'unknown', tokens_used: null, outcome_reason: 'provider_outcome_unknown' };
  const newRun = { ...run, id: 'run-2', work_order_title: 'Revised note',
    work_order_instructions: 'Write a short answer' };
  const newOutput = { ...output, id: 'output-2', run_id: newRun.id };
  let restarted = false;
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/model/providers')) return json(providers);
    if (path.endsWith('/model/accounts')) return json([modelAccount]);
    if (path.endsWith('/model/catalog')) return json(catalog);
    if (path.endsWith('/work-orders/work-1') && init?.method === 'PUT') {
      expect(JSON.parse(init.body as string)).toEqual({ title: 'Revised note',
        instructions: 'Write a short answer', expected_version: 4 });
      savedOrder = { ...savedOrder, title: 'Revised note', instructions: 'Write a short answer', version: 5 };
      return json(savedOrder);
    }
    if (path.endsWith('/work-orders/work-1/live-text-restart') && init?.method === 'POST') {
      expect(JSON.parse(init.body as string)).toEqual({ prior_run_id: oldRun.id,
        expected_version: 5, acknowledge_unknown: true });
      restarted = true;
      savedOrder = { ...savedOrder, status: 'done', version: 7 };
      return json({ run: newRun, output: newOutput }, 201);
    }
    if (path.endsWith('/work-orders')) return json([savedOrder]);
    if (path.endsWith('/runs/run-2/live-text-output')) return json(newOutput);
    if (path.endsWith('/runs')) return json(restarted ? [newRun, oldRun] : [oldRun]);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  const onOpenRun = vi.fn();
  render(<WorkOrdersView projectId={projectId} profiles={[profile]} onOpenRun={onOpenRun} />);
  const restart = await screen.findByRole('button', { name: 'Restart as new live-text attempt' });
  expect(restart).toBeDisabled();
  expect(screen.queryByRole('button', { name: 'Run live text with provider' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Edit work order' }));
  await user.clear(screen.getByLabelText('Title for new attempt'));
  await user.type(screen.getByLabelText('Title for new attempt'), 'Revised note');
  await user.clear(screen.getByLabelText('Instructions for new attempt'));
  await user.type(screen.getByLabelText('Instructions for new attempt'), 'Write a short answer');
  await user.click(screen.getByRole('button', { name: 'Save edit' }));
  expect(await screen.findByText(/earlier unknown run keeps its original task snapshot/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Restart as new live-text attempt' })).toBeDisabled();
  await user.click(screen.getByRole('checkbox', { name: /earlier call may have completed/ }));
  await user.click(screen.getByRole('button', { name: 'Restart as new live-text attempt' }));
  await waitFor(() => expect(onOpenRun).toHaveBeenCalledWith('run-2'));
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/live-text-restart'))).toHaveLength(1);
  expect(oldRun.work_order_title).toBe('Write a note');
});

it('shows live provider/model, generated text, token usage, and unknown cost', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.endsWith('/runs/run-1/live-text-output')) return json(output);
    if (path.endsWith('/runs/run-1')) return json({ ...run, events: [{
      id: 'event-1', project_id: projectId, work_order_id: order.id, run_id: run.id,
      event_type: 'live_text_started', actor_id: 'agent-1', created_at: time,
      payload: { tools_active: false, skills: [
        { skill_id: 'skill-1', version: 1, sha256: 'a'.repeat(64), selection_source: 'automatic' },
        { skill_id: 'skill-2', version: 2, sha256: 'b'.repeat(64), selection_source: 'pinned' },
      ] },
    }], profile_snapshots: [], skill_snapshots: [{
      skill_id: 'skill-1', skill_name: 'Research summary', version: 1, sha256: 'a'.repeat(64),
      transition_index: 0, actor_id: 'agent-1', created_at: time,
    }, {
      skill_id: 'skill-2', skill_name: 'Style guide', version: 2, sha256: 'b'.repeat(64),
      transition_index: 0, actor_id: 'agent-1', created_at: time,
    }] });
    if (path.endsWith('/runs')) return json([run]);
    throw new Error(`Unexpected path ${path}`);
  }));
  render(<RunTimelineView projectId={projectId} profiles={[profile]} initialRunId="run-1" />);
  expect(await screen.findByText('A concise plan.')).toBeInTheDocument();
  expect(screen.getAllByText('openai · text-a').length).toBeGreaterThan(0);
  expect(screen.getByText('27 tokens · Cost not available')).toBeInTheDocument();
  expect(screen.getByText('Input 20 tokens · Output 7 tokens · Cost not available')).toBeInTheDocument();
  const inference = screen.getByRole('region', { name: 'Inference log' });
  expect(inference).not.toHaveTextContent('run-1');
  expect(inference).toHaveTextContent('openai · text-a');
  expect(inference).not.toHaveTextContent('response-1');
  expect(inference).toHaveTextContent('OneFoundry role toolsInactive for this run');
  expect(inference).toHaveTextContent('Any proposed actions in its text were not executed by this run.');
  expect(screen.getByText(/Done confirms the model response, not completion of the requested deliverable/)).toBeInTheDocument();
  const procedures = screen.getByRole('region', { name: 'Procedures supplied to model' });
  expect(procedures).toHaveTextContent('Research summary · v1');
  expect(procedures).toHaveTextContent('Automatic · Assignment 0');
  expect(procedures).toHaveTextContent('Style guide · v2');
  expect(procedures).toHaveTextContent('Pinned · Assignment 0');
});

it('rejects a live output from another project', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ ...output, project_id: 'project-2' })));
  await expect(getLiveTextOutput(projectId, run.id)).rejects.toThrow('invalid live output scope');
});

it('keeps provider error run ID available for reconciliation', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ detail: 'live inference failed', run_id: 'run-1' }, 502)));
  await expect(runLiveText(projectId, order.id)).rejects.toMatchObject({ status: 502, runId: 'run-1' });
});

it('hides live action when the bound model is not a supported text route', async () => {
  const fetcher = vi.fn(async (path: string) => {
    if (path.endsWith('/model/providers')) return json(providers);
    if (path.endsWith('/model/accounts')) return json([modelAccount]);
    if (path.endsWith('/model/catalog')) return json([{ ...catalog[0], category: 'image' }]);
    if (path.endsWith('/work-orders')) return json([order]);
    if (path.endsWith('/runs')) return json([]);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  render(<WorkOrdersView projectId={projectId} profiles={[profile]} onOpenRun={() => {}} />);
  expect(await screen.findByText(/Live text unavailable for this model/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Run live text with provider' })).not.toBeInTheDocument();
});

it('preserves unknown token usage and displays exact xAI cost ticks', async () => {
  const grokOutput = { ...output, provider: 'grok', input_tokens: null, output_tokens: null,
    cost_usd: 0.0123456789, cost_usd_ticks: 123456789 };
  const grokRun = { ...run, tokens_used: null };
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.endsWith('/runs/run-1/live-text-output')) return json(grokOutput);
    if (path.endsWith('/runs/run-1')) return json({ ...grokRun, events: [], profile_snapshots: [] });
    if (path.endsWith('/runs')) return json([grokRun]);
    throw new Error(`Unexpected path ${path}`);
  }));
  render(<RunTimelineView projectId={projectId} profiles={[profile]} initialRunId="run-1" />);
  expect(await screen.findByText('Usage unavailable · Cost $0.0123456789')).toBeInTheDocument();
  expect(screen.getByText('Input usage unavailable · Output usage unavailable · Cost $0.0123456789')).toBeInTheDocument();
});

it('does not claim generated output for an unknown provider run', async () => {
  const unknownRun = { ...run, status: 'unknown', tokens_used: null, outcome_reason: 'Provider outcome unknown' };
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.endsWith('/runs/run-1')) return json({ ...unknownRun, events: [{
      id: 'event-1', project_id: projectId, work_order_id: order.id, run_id: run.id,
      event_type: 'live_text_started', actor_id: 'operator', created_at: time,
      payload: { request_id: run.id, provider: 'openai_codex_runtime', model: 'gpt-6-sol' },
    }, {
      id: 'event-2', project_id: projectId, work_order_id: order.id, run_id: run.id,
      event_type: 'live_text_finished', actor_id: 'agent-1', created_at: time,
      payload: { status: 'unknown', diagnostic_code: 'provider_http_404' },
    }], profile_snapshots: [] });
    if (path.endsWith('/runs')) return json([unknownRun]);
    throw new Error(`Unexpected path ${path}`);
  }));
  render(<RunTimelineView projectId={projectId} profiles={[profile]} initialRunId="run-1" />);
  expect(await screen.findByText(/no confirmed saved text output/)).toBeInTheDocument();
  expect(screen.queryByText('The configured provider generated this text.')).not.toBeInTheDocument();
  expect(screen.getByText('Usage unavailable · Cost not available')).toBeInTheDocument();
  const inference = screen.getByRole('region', { name: 'Inference log' });
  expect(inference).toHaveTextContent('openai_codex_runtime · gpt-6-sol');
  expect(inference).toHaveTextContent('provider http 404');
  expect(inference).toHaveTextContent('unknown');
  expect(inference).toHaveTextContent('will not retry it automatically');
});
