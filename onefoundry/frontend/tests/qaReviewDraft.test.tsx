import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import type { Profile } from '../src/api';
import { RunTimelineView } from '../src/RunTimelineView';
import type { Run, RunEvent, WorkOrder } from '../src/workApi';

const projectId = 'project-1';
const run: Run = {
  id: 'run-1', project_id: projectId, work_order_id: 'work-1',
  work_order_title: 'Implement change', work_order_instructions: 'Make the change',
  profile_id: 'engineer-1', profile_model: null, status: 'done', mode: 'manual_planning',
  tokens_used: 0, cost_usd: 0, outcome_reason: 'Implementation saved',
  started_at: '2026-09-28T12:00:00Z', finished_at: '2026-09-28T12:01:00Z',
};
const qaProfile: Profile = {
  id: 'qa-1', project_id: projectId, name: 'QA', kind: 'builtin', reports_to_profile_id: null,
  model: null, instructions: '', memory_scope: 'company', allowed_tools: [], autonomy_level: 'manual',
  configured: false, permissions_active: false,
};
const requestEvent: RunEvent = {
  id: 'request-1', project_id: projectId, work_order_id: run.work_order_id,
  run_id: run.id, event_type: 'qa_review_requested', actor_id: 'engineer-1',
  payload: {}, created_at: run.finished_at!,
};
const draft: WorkOrder = {
  id: 'qa-work-1', project_id: projectId, profile_id: qaProfile.id, title: 'Review change',
  instructions: 'Check saved diff and tests', status: 'draft', version: 1,
  deliverable_kind: 'operator_review', acceptance_checks: [], parent_work_order_id: null, source_run_id: null, origin_signal_id: null, chain_depth: 0,
  block_reason: null, blocked_from_status: null, created_at: requestEvent.created_at,
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

afterEach(() => vi.unstubAllGlobals());

it('creates an operator supplied QA draft from a same-run request and confirms its saved owner state', async () => {
  const orders: WorkOrder[] = [];
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${projectId}/runs`) return json([run]);
    if (path === `/api/projects/${projectId}/runs/${run.id}`) return json({ ...run, events: [requestEvent], profile_snapshots: [], skill_snapshots: [] });
    if (path === `/api/projects/${projectId}/results`) return json([]);
    if (path === `/api/projects/${projectId}/work-orders`) return json(orders);
    if (path === `/api/projects/${projectId}/runs/${run.id}/qa-review-requests/${requestEvent.id}/draft` && !init?.method) return json(null);
    if (path === `/api/projects/${projectId}/runs/${run.id}/qa-review-requests/${requestEvent.id}/draft` && init?.method === 'POST') {
      expect(JSON.parse(init.body as string)).toEqual({ title: draft.title, instructions: draft.instructions });
      orders.push(draft);
      return json(draft);
    }
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<RunTimelineView projectId={projectId} profiles={[qaProfile]} initialRunId={run.id} />);

  expect(await screen.findByText('QA review requested')).toBeInTheDocument();
  await user.type(screen.getByLabelText('QA draft title'), draft.title);
  await user.type(screen.getByLabelText('QA draft instructions'), draft.instructions);
  await user.click(screen.getByRole('button', { name: 'Create QA draft' }));

  expect(await screen.findByRole('status')).toHaveTextContent('QA draft saved');
  expect(screen.getByRole('status')).toHaveTextContent(draft.title);
  expect(screen.getByRole('status')).not.toHaveTextContent(draft.id);
  expect(screen.getByRole('status')).toHaveTextContent('Open Work orders');
  expect(fetcher.mock.calls.filter(([path, init]) => path.endsWith(`/qa-review-requests/${requestEvent.id}/draft`) && init?.method === 'POST')).toHaveLength(1);
  expect(fetcher.mock.calls.some(([path]) => path === `/api/projects/${projectId}/work-orders`)).toBe(true);
  expect(screen.queryByRole('button', { name: 'Create QA draft' })).not.toBeInTheDocument();
});

it('hides the draft action for a request event belonging to another run', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path === `/api/projects/${projectId}/runs`) return json([run]);
    if (path === `/api/projects/${projectId}/runs/${run.id}`) return json({ ...run,
      events: [{ ...requestEvent, run_id: 'run-elsewhere' }], profile_snapshots: [], skill_snapshots: [] });
    if (path === `/api/projects/${projectId}/results`) return json([]);
    throw new Error(`Unexpected path ${path}`);
  }));
  render(<RunTimelineView projectId={projectId} profiles={[qaProfile]} initialRunId={run.id} />);
  expect(await screen.findByText('QA review requested')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Create QA draft' })).not.toBeInTheDocument();
});

it('does not report success when the saved work order readback disagrees', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${projectId}/runs`) return json([run]);
    if (path === `/api/projects/${projectId}/runs/${run.id}`) return json({ ...run, events: [requestEvent], profile_snapshots: [], skill_snapshots: [] });
    if (path === `/api/projects/${projectId}/results`) return json([]);
    if (path === `/api/projects/${projectId}/work-orders`) return json([{ ...draft, status: 'ready' }]);
    if (path.endsWith(`/qa-review-requests/${requestEvent.id}/draft`) && !init?.method) return json(null);
    if (path.endsWith(`/qa-review-requests/${requestEvent.id}/draft`) && init?.method === 'POST') return json(draft);
    throw new Error(`Unexpected path ${path}`);
  }));
  const user = userEvent.setup();
  render(<RunTimelineView projectId={projectId} profiles={[qaProfile]} initialRunId={run.id} />);
  await user.type(await screen.findByLabelText('QA draft title'), draft.title);
  await user.type(screen.getByLabelText('QA draft instructions'), draft.instructions);
  await user.click(screen.getByRole('button', { name: 'Create QA draft' }));
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('readback did not confirm'));
  expect(screen.queryByText(/QA draft saved/)).not.toBeInTheDocument();
});

it('shows the saved QA draft after opening the run again without offering another create action', async () => {
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${projectId}/runs`) return json([run]);
    if (path === `/api/projects/${projectId}/runs/${run.id}`) return json({ ...run, events: [requestEvent], profile_snapshots: [], skill_snapshots: [] });
    if (path === `/api/projects/${projectId}/results`) return json([]);
    if (path === `/api/projects/${projectId}/runs/${run.id}/qa-review-requests/${requestEvent.id}/draft` && !init?.method) return json(draft);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  render(<RunTimelineView projectId={projectId} profiles={[qaProfile]} initialRunId={run.id} />);
  expect(await screen.findByText(`QA draft “${draft.title}” is saved. Open Work orders to review it.`)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Create QA draft' })).not.toBeInTheDocument();
  expect(fetcher.mock.calls.filter(([path, init]) => path.endsWith(`/qa-review-requests/${requestEvent.id}/draft`) && init?.method === 'POST')).toHaveLength(0);
});

it('keeps operator text separate for two QA requests on one run', async () => {
  const second = { ...requestEvent, id: 'request-2' };
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path === `/api/projects/${projectId}/runs`) return json([run]);
    if (path === `/api/projects/${projectId}/runs/${run.id}`) return json({ ...run,
      events: [requestEvent, second], profile_snapshots: [], skill_snapshots: [] });
    if (path === `/api/projects/${projectId}/results`) return json([]);
    if (path.endsWith(`/qa-review-requests/${requestEvent.id}/draft`) || path.endsWith(`/qa-review-requests/${second.id}/draft`)) return json(null);
    throw new Error(`Unexpected path ${path}`);
  }));
  const user = userEvent.setup();
  render(<RunTimelineView projectId={projectId} profiles={[qaProfile]} initialRunId={run.id} />);
  const titles = await screen.findAllByLabelText('QA draft title');
  await user.type(titles[0], 'First review');
  expect(titles[1]).toHaveValue('');
  await user.type(titles[1], 'Second review');
  expect(titles[0]).toHaveValue('First review');
});

it('does not offer create when saved draft lookup fails, and allows a readback retry', async () => {
  let fail = true;
  const fetcher = vi.fn(async (path: string) => {
    if (path === `/api/projects/${projectId}/runs`) return json([run]);
    if (path === `/api/projects/${projectId}/runs/${run.id}`) return json({ ...run, events: [requestEvent], profile_snapshots: [], skill_snapshots: [] });
    if (path === `/api/projects/${projectId}/results`) return json([]);
    if (path.endsWith(`/qa-review-requests/${requestEvent.id}/draft`)) return fail ? json({ detail: 'Draft readback unavailable' }, 503) : json(null);
    throw new Error(`Unexpected path ${path}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<RunTimelineView projectId={projectId} profiles={[qaProfile]} initialRunId={run.id} />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Draft readback unavailable');
  expect(screen.queryByRole('button', { name: 'Create QA draft' })).not.toBeInTheDocument();
  fail = false;
  await user.click(screen.getByRole('button', { name: 'Retry run readback' }));
  expect(await screen.findByRole('button', { name: 'Create QA draft' })).toBeInTheDocument();
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith(`/qa-review-requests/${requestEvent.id}/draft`))).toHaveLength(2);
});
