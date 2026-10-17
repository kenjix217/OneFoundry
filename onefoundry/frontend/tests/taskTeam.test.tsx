import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import type { Profile } from '../src/api';
import { TaskTeamView } from '../src/TaskTeamView';
import type { Run, WorkOrder } from '../src/workApi';

const projectId = 'project-1';
const profiles = [
  { id: 'chief', name: 'Chief of Staff' }, { id: 'researcher', name: 'Researcher' },
] as Profile[];
const root = { id: 'root', project_id: projectId, title: 'Launch demo', instructions: 'Deliver a playable demo.',
  profile_id: 'chief', parent_work_order_id: null, source_run_id: null, status: 'running',
  acceptance_checks: ['Playable file'] } as WorkOrder;
const child = { id: 'child', project_id: projectId, title: 'Research product',
  instructions: 'Document safe product claims.', profile_id: 'researcher', parent_work_order_id: 'root',
  source_run_id: 'run-chief', status: 'ready', acceptance_checks: ['Evidence saved'] } as WorkOrder;
const run = { id: 'run-child', project_id: projectId, work_order_id: 'child', profile_id: 'researcher',
  status: 'done', mode: 'live_text', started_at: '2026-10-03T12:00:00Z' } as Run;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

afterEach(() => vi.unstubAllGlobals());

it('distinguishes a proposed plan from a saved team assignment', () => {
  render(<TaskTeamView projectId={projectId} order={root} orders={[root]} runs={[]}
    profiles={profiles} onOpenTask={() => {}} onOpenRun={() => {}} />);
  expect(screen.getByText(/No team assignments are saved/)).toBeInTheDocument();
  expect(screen.queryByText(/Researcher said/)).not.toBeInTheDocument();
});

it('shows linked tasks and opens only saved child chat turns and runs', async () => {
  const onOpenTask = vi.fn();
  const onOpenRun = vi.fn();
  const fetcher = vi.fn(async (url: string) => {
    if (url === `/api/projects/${projectId}/work-orders/child/conversations`) return json([{
      id: 'chat-child', project_id: projectId, work_order_id: 'child', profile_id: 'chief',
      title: 'Research notes', is_active: true, status: 'open', created_at: '2026-10-03T12:00:00Z',
    }]);
    if (url === `/api/projects/${projectId}/work-orders/child/conversations/chat-child`) return json({
      id: 'chat-child', project_id: projectId, work_order_id: 'child', profile_id: 'chief',
      title: 'Research notes', is_active: true, status: 'open', turns: [{
        id: 'turn-child', client_turn_id: 'submission', sequence: 1, status: 'done',
        source_kind: 'operator_ui', operator_text: 'Check the claims.', assistant_text: 'Two claims need evidence.',
        assistant_actor_id: 'profile:chief', reason: null, provider: 'openai', model_id: 'test',
        input_tokens: 3, output_tokens: 5, cost_usd: null, created_at: '2026-10-03T12:01:00Z',
        finished_at: '2026-10-03T12:01:02Z',
      }],
    });
    throw new Error(`Unexpected URL ${url}`);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<TaskTeamView projectId={projectId} order={root} orders={[root, child]} runs={[run]}
    profiles={profiles} onOpenTask={onOpenTask} onOpenRun={onOpenRun} />);
  const card = screen.getByRole('article', { name: 'Research product' });
  expect(within(card).getByText('Document safe product claims.')).toBeInTheDocument();
  expect(within(card).getByText('Saved assignment')).toBeInTheDocument();
  await user.click(within(card).getByRole('button', { name: 'View saved chats' }));
  expect(await within(card).findByText('Two claims need evidence.')).toBeInTheDocument();
  expect(within(card).getByText('Research notes · Chief of Staff')).toBeInTheDocument();
  expect(within(card).getByText('Check the claims.')).toBeInTheDocument();
  await user.click(within(card).getByRole('button', { name: 'Open task' }));
  await user.click(within(card).getByRole('button', { name: /Open run 1/ }));
  expect(onOpenTask).toHaveBeenCalledWith('child');
  expect(onOpenRun).toHaveBeenCalledWith('run-child');
});

it('does not present failed chat reads as no agent conversation', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ detail: 'unavailable' }, 503)));
  const user = userEvent.setup();
  render(<TaskTeamView projectId={projectId} order={root} orders={[root, child]} runs={[]}
    profiles={profiles} onOpenTask={() => {}} onOpenRun={() => {}} />);
  await user.click(within(screen.getByRole('article', { name: 'Research product' }))
    .getByRole('button', { name: 'View saved chats' }));
  expect(await screen.findByRole('alert')).toHaveTextContent(/unavailable|failed/i);
  expect(screen.queryByText('No saved chats for this assignment.')).not.toBeInTheDocument();
});
