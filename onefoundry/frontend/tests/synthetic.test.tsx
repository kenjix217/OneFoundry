vi.mock('../src/taskWorkflowApi', () => ({
  activateTeam: vi.fn(),
  getTeamReadiness: vi.fn(async (_project: string, order: string) => ({
    work_order_id: order, status: 'draft', execution_mode: 'legacy', worker_enabled: false,
    provider: null, ready_to_activate: false, blockers: ['The team worker is disabled.'],
    pending_approvals: 0, queued_attempts: 0, running_attempts: 0, confirmed_results: 0,
    next_step: 'Review configured execution in Team.',
  })),
}));

import { afterEach, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import type { Profile } from '../src/api';
import { RunTimelineView } from '../src/RunTimelineView';
import { WorkOrdersView } from '../src/WorkOrdersView';
import { getTextOutputs, type Run, type RunEvent, type TextOutput, type WorkOrder } from '../src/workApi';

const projectId = 'project-1';
const time = '2026-09-25T12:00:00Z';
const profiles: Profile[] = [
  { id: 'researcher-1', project_id: projectId, name: 'Researcher', kind: 'builtin', reports_to_profile_id: null, model: null,
    instructions: '', memory_scope: 'company', allowed_tools: [], autonomy_level: 'manual',
    configured: false, permissions_active: false },
  { id: 'gtm-1', project_id: projectId, name: 'GTM', kind: 'builtin', reports_to_profile_id: null, model: null,
    instructions: '', memory_scope: 'company', allowed_tools: [], autonomy_level: 'manual',
    configured: false, permissions_active: false },
];
const order: WorkOrder = { id: 'work-1', project_id: projectId, title: 'Explore launch message',
  instructions: 'Use approved local context', profile_id: 'researcher-1', status: 'ready',
  deliverable_kind: 'operator_review', acceptance_checks: [], parent_work_order_id: null, source_run_id: null, origin_signal_id: null, chain_depth: 0,
  version: 1, block_reason: null, blocked_from_status: null, created_at: time };
const run: Run = { id: 'run-1', project_id: projectId, work_order_id: order.id,
  work_order_title: order.title, work_order_instructions: order.instructions,
  profile_id: 'gtm-1', profile_model: null, status: 'done', mode: 'synthetic_text',
  tokens_used: 0, cost_usd: 0, outcome_reason: '', started_at: time, finished_at: time };
const outputs: TextOutput[] = [
  { id: 'output-1', project_id: projectId, run_id: run.id, profile_id: 'researcher-1', actor_id: 'agent:researcher',
    role: 'Researcher', source_kind: 'synthetic_scripted_sdk', configured_model: null,
    text: 'Synthetic research finding from approved memory.', source_memory_ids: ['memory-1'],
    source_memories: [{ id: 'memory-1', project_id: projectId, scope: 'project',
      text: 'Approved source phrase', source_result_id: 'result-1' }],
    tokens_used: 0, cost_usd: 0, created_at: time },
  { id: 'output-2', project_id: projectId, run_id: run.id, profile_id: 'gtm-1', actor_id: 'agent:gtm',
    role: 'GTM', source_kind: 'synthetic_scripted_sdk', configured_model: null,
    text: 'Synthetic GTM draft for operator review.', source_memory_ids: [], source_memories: [],
    tokens_used: 0, cost_usd: 0, created_at: time },
];
const events: RunEvent[] = [
  { id: 'event-1', project_id: projectId, work_order_id: order.id, run_id: run.id,
    actor_id: 'agent:researcher', event_type: 'synthetic_text_started', payload: { status: 'running' }, created_at: time },
  { id: 'event-2', project_id: projectId, work_order_id: order.id, run_id: run.id,
    actor_id: 'agent:gtm', event_type: 'synthetic_text_handed_off', payload: { from_profile_id: 'researcher-1', to_profile_id: 'gtm-1' }, created_at: time },
  { id: 'event-3', project_id: projectId, work_order_id: order.id, run_id: run.id,
    actor_id: 'agent:gtm', event_type: 'synthetic_text_finished', payload: { status: 'done' }, created_at: time },
];

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

afterEach(() => vi.unstubAllGlobals());

it('runs the eligible local fixture once, confirms saved outputs, and shows cited Researcher to GTM readback', async () => {
  let currentOrder = { ...order };
  let savedRun: Run | null = null;
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${projectId}/work-orders`) return json([currentOrder]);
    if (path === `/api/projects/${projectId}/work-orders/work-1/synthetic-text-eligibility`) return json({
      eligible: true, reason: null, fixture: 'research_to_gtm',
      researcher_profile_id: 'researcher-1', gtm_profile_id: 'gtm-1',
    });
    if (path === `/api/projects/${projectId}/work-orders/work-1/synthetic-text-run` && init?.method === 'POST') {
      expect(JSON.parse(init.body as string)).toEqual({ fixture: 'research_to_gtm' });
      currentOrder = { ...currentOrder, status: 'done' };
      savedRun = run;
      return json({ run, outputs }, 201);
    }
    if (path === `/api/projects/${projectId}/runs`) return json(savedRun ? [savedRun] : []);
    if (path === `/api/projects/${projectId}/runs/run-1`) return json({ ...run, events });
    if (path === `/api/projects/${projectId}/runs/run-1/text-outputs`) return json(outputs);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  const onOpenRun = vi.fn();
  const work = render(<WorkOrdersView projectId={projectId} profiles={profiles} onOpenRun={onOpenRun} />);
  await user.click(await screen.findByRole('button', { name: 'Run synthetic text fixture' }));
  expect(onOpenRun).toHaveBeenCalledWith(run.id);
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/synthetic-text-run'))).toHaveLength(1);
  work.unmount();

  render(<RunTimelineView projectId={projectId} profiles={profiles} initialRunId={run.id} />);
  expect(await screen.findByRole('heading', { name: 'Researcher → GTM text' })).toBeInTheDocument();
  expect(screen.getByText('Synthetic research finding from approved memory.')).toBeInTheDocument();
  expect(screen.getByText('Synthetic GTM draft for operator review.')).toBeInTheDocument();
  expect(screen.getByText('Approved source phrase')).toBeInTheDocument();
  expect(screen.getByText('Fixture work handed to the next agent')).toBeInTheDocument();
  expect(screen.getByText('Synthetic fixture · 0 provider tokens · $0.0000 provider cost')).toBeInTheDocument();
  expect(screen.getByText(/This is fixture output, not a live AI result/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Record outcome' })).not.toBeInTheDocument();
});

it('shows server ineligibility without offering a fixture write', async () => {
  const fetcher = vi.fn(async (path: string) => {
    if (path === `/api/projects/${projectId}/work-orders`) return json([order]);
    if (path === `/api/projects/${projectId}/runs`) return json([]);
    if (path.endsWith('/synthetic-text-eligibility')) return json({ eligible: false,
      reason: 'GTM profile is missing', fixture: 'research_to_gtm',
      researcher_profile_id: 'researcher-1', gtm_profile_id: null });
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  render(<WorkOrdersView projectId={projectId} profiles={profiles} onOpenRun={vi.fn()} />);
  expect(await screen.findByText('Synthetic text fixture unavailable: GTM profile is missing')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Run synthetic text fixture' })).not.toBeInTheDocument();
  expect(fetcher.mock.calls.some(([path]) => path.endsWith('/synthetic-text-run'))).toBe(false);
});

it('rejects a mismatched text-output owner instead of showing another project output', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json([{ ...outputs[0], project_id: 'other-project' }])));
  await expect(getTextOutputs(projectId, run.id)).rejects.toThrow('invalid text output scope');
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json([{ ...outputs[0], source_memories: [
    { ...outputs[0].source_memories[0], project_id: 'other-project' },
  ] }])));
  await expect(getTextOutputs(projectId, run.id)).rejects.toThrow('invalid text output scope');
});

it('keeps a 409 conflict visible and does not retry a synthetic write', async () => {
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${projectId}/work-orders`) return json([order]);
    if (path === `/api/projects/${projectId}/runs`) return json([]);
    if (path.endsWith('/synthetic-text-eligibility')) return json({ eligible: true, reason: null,
      fixture: 'research_to_gtm', researcher_profile_id: 'researcher-1', gtm_profile_id: 'gtm-1' });
    if (path.endsWith('/synthetic-text-run') && init?.method === 'POST') return json({ detail: 'work order already ran' }, 409);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<WorkOrdersView projectId={projectId} profiles={profiles} onOpenRun={vi.fn()} />);
  await user.click(await screen.findByRole('button', { name: 'Run synthetic text fixture' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('work order already ran');
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/synthetic-text-run'))).toHaveLength(1);
});
