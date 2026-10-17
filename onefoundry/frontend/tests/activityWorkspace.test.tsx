import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { RunTimelineView } from '../src/RunTimelineView';

const projectId = 'project-1';
const orderId = 'work-1';
const started = '2026-09-25T12:00:00Z';
const run = { id: 'run-1', project_id: projectId, work_order_id: orderId,
  work_order_title: 'Prepare a brief', work_order_instructions: 'A long work order brief',
  profile_id: 'profile-1', profile_model: null, status: 'running', mode: 'manual_planning',
  tokens_used: 0, cost_usd: 0, outcome_reason: '', started_at: started, finished_at: null };
const order = { id: orderId, project_id: projectId, title: 'Prepare a brief', instructions: run.work_order_instructions,
  profile_id: 'profile-1', status: 'running', version: 1, block_reason: null, blocked_from_status: null,
  created_at: started };
const event = { id: 'event-1', project_id: projectId, work_order_id: orderId, run_id: 'run-1',
  actor_id: 'operator', event_type: 'run_started', payload: { origin: 'manual' }, created_at: started };

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
}

afterEach(() => vi.unstubAllGlobals());

it('shows a task-scoped activity list with compact details and a direct return to Task', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path === `/api/projects/${projectId}/runs`) return json([run]);
    if (path === `/api/projects/${projectId}/runs/run-1`) return json({ ...run, events: [event],
      profile_snapshots: [], skill_snapshots: [] });
    if (path === `/api/projects/${projectId}/work-orders`) return json([order]);
    throw new Error(`Unexpected path ${path}`);
  }));
  const openTask = vi.fn();
  const user = userEvent.setup();
  const { container } = render(<RunTimelineView projectId={projectId} profiles={[]} initialRunId={null}
    initialOrderId={orderId} onOpenTask={openTask} />);
  expect(await screen.findByRole('heading', { name: 'Activity' })).toBeInTheDocument();
  await waitFor(() => expect(screen.getByRole('button', { name: 'Open task →' })).toBeInTheDocument());
  const brief = container.querySelector('.activity-detail-group');
  expect(brief).not.toHaveAttribute('open');
  await user.click(screen.getByText('Work order brief and run details'));
  expect(brief).toHaveAttribute('open');
  expect(screen.getByText('A long work order brief')).toBeVisible();
  await user.click(screen.getByRole('button', { name: '← Task' }));
  expect(openTask).toHaveBeenCalledWith(orderId);
});
