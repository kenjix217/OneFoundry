import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { TaskWorkflowPanel } from '../src/TaskWorkflowPanel';
import { activateTeam, cancelQueuedTeam, getTeamReadiness, type TeamReadiness } from '../src/taskWorkflowApi';

vi.mock('../src/taskWorkflowApi', () => ({ activateTeam: vi.fn(), cancelQueuedTeam: vi.fn(), getTeamReadiness: vi.fn() }));
const state: TeamReadiness = { work_order_id: 'order', status: 'draft', execution_mode: 'legacy', worker_enabled: false,
  provider: 'openai_codex_runtime', ready_to_activate: false, blockers: ['The team worker is disabled.', 'Unregistered agent capabilities: video_edit.'],
  pending_approvals: 0, queued_attempts: 0, running_attempts: 0, confirmed_results: 0, next_step: 'Enable worker.' };
afterEach(() => vi.clearAllMocks());

it.each([[null, 'Unlimited'], [16384, '16,384 bytes']] as const)(
  'displays the saved prompt limit %s without starting work', async (limit, label) => {
    vi.mocked(getTeamReadiness).mockResolvedValue({ ...state, prompt_context_bytes_limit: limit });
    render(<TaskWorkflowPanel projectId="project" orderId="order" version={1} reloadKey={0} />);
    expect(await screen.findByText(`Prompt limit: ${label}`)).toBeInTheDocument();
    expect(activateTeam).not.toHaveBeenCalled();
  }
);

it('shows the draft blockers and zero execution without starting anything', async () => {
  vi.mocked(getTeamReadiness).mockResolvedValue(state);
  render(<TaskWorkflowPanel projectId="project" orderId="order" version={1} reloadKey={0} />);
  expect(await screen.findByText('The team worker is disabled.')).toBeInTheDocument();
  expect(screen.getByText(/0 active · 0 queued/)).toBeInTheDocument();
  expect(screen.getByText(/Unregistered agent capabilities/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Enable team execution' })).not.toBeInTheDocument();
  expect(activateTeam).not.toHaveBeenCalled();
});

it('requires an explicit enable click and refreshes after the saved cutover', async () => {
  vi.mocked(getTeamReadiness).mockResolvedValue({ ...state, worker_enabled: true, status: 'ready', blockers: [], ready_to_activate: true });
  vi.mocked(activateTeam).mockResolvedValue();
  const refreshed = vi.fn();
  render(<TaskWorkflowPanel projectId="project" orderId="order" version={4} reloadKey={0} onActivated={refreshed} />);
  const button = await screen.findByRole('button', { name: 'Enable team execution' });
  expect(activateTeam).not.toHaveBeenCalled();
  await userEvent.setup().click(button);
  await waitFor(() => expect(activateTeam).toHaveBeenCalledWith('project', 'order', 4));
  expect(refreshed).toHaveBeenCalledOnce();
});

it('surfaces a dependency failure instead of presenting readiness as success', async () => {
  vi.mocked(getTeamReadiness).mockRejectedValue(new Error('Database unavailable'));
  render(<TaskWorkflowPanel projectId="project" orderId="order" version={1} reloadKey={0} />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Database unavailable');
  expect(screen.queryByRole('button')).not.toBeInTheDocument();
});

it('offers an explicit start for an admitted draft without silently launching it', async () => {
  vi.mocked(getTeamReadiness).mockResolvedValue({ ...state, worker_enabled: true, ready_to_start: true });
  const start = vi.fn();
  render(<TaskWorkflowPanel projectId="project" orderId="order" version={1} reloadKey={0} onStartWork={start} />);
  const button = await screen.findByRole('button', { name: 'Start work' });
  expect(start).not.toHaveBeenCalled();
  await userEvent.setup().click(button);
  expect(start).toHaveBeenCalledOnce();
  expect(activateTeam).not.toHaveBeenCalled();
});

it('cancels only queued work after an explicit click', async () => {
  vi.mocked(getTeamReadiness).mockResolvedValue({ ...state, status: 'ready', execution_mode: 'team', queued_attempts: 1 });
  vi.mocked(cancelQueuedTeam).mockResolvedValue();
  const refreshed = vi.fn();
  render(<TaskWorkflowPanel projectId="project" orderId="order" version={3} reloadKey={0} onActivated={refreshed} />);
  const button = await screen.findByRole('button', { name: 'Cancel queued work' });
  expect(cancelQueuedTeam).not.toHaveBeenCalled();
  await userEvent.setup().click(button);
  expect(cancelQueuedTeam).toHaveBeenCalledWith('project', 'order', 3);
  expect(refreshed).toHaveBeenCalledOnce();
});
