import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ApiError, type Profile } from '../src/api';
import { TaskSessionView } from '../src/TaskSessionView';
import { getTeamSession, returnTeamChild, sendTeamMessage, type TeamSessionView } from '../src/taskSessionApi';

vi.mock('../src/taskSessionApi', () => ({
  getTeamSession: vi.fn(), sendTeamMessage: vi.fn(), continueTeamSession: vi.fn(),
  reconcileTeamActions: vi.fn(), acknowledgeUnknown: vi.fn(), returnTeamChild: vi.fn(),
}));
const chief: Profile = { id: 'chief', project_id: 'project', kind: 'builtin', name: 'Chief of Staff',
  reports_to_profile_id: null, model: 'fixture', instructions: 'Coordinate', memory_scope: 'project',
  allowed_tools: [], autonomy_level: 'supervised', configured: true, permissions_active: false };
const worker: Profile = { ...chief, id: 'worker', name: 'Producer', reports_to_profile_id: 'chief' };
const child: TeamSessionView = {
  session: { id: 'child', profile_id: 'worker', status: 'closed', execution_mode: 'team', work_order_status: 'needs_review' },
  messages: [{ id: 'old-result', session_id: 'child', client_message_id: null, sequence: 7, actor_id: 'profile:worker',
    kind: 'agent', body: 'Previous result saved with a source limitation.', created_at: '2026-10-06T20:00:00Z' }],
  attempts: [], children: [], assignments: [], decision_causes: [], tool_calls: [],
};
const root: TeamSessionView = { ...child,
  session: { id: 'main', profile_id: 'chief', status: 'open', execution_mode: 'team', work_order_status: 'running' },
  messages: [], children: [{ id: 'child', profile_id: 'worker', parent_session_id: 'main', status: 'closed', work_order_status: 'needs_review', execution_mode: 'team' }],
  assignments: [{ id: 'assignment', child_session_id: 'child', objective: 'Create a sourced briefing',
    expected_result: 'Saved source-backed artifact', acceptance_check: 'Verify the actual artifact', acceptance_kind: 'artifact',
    status: 'satisfied', result_message_id: 'old-result' }],
};
function readClosed() {
  vi.mocked(getTeamSession).mockImplementation(async (_project, _order, sessionId) => sessionId ? child : root);
}
afterEach(() => { vi.clearAllMocks(); sessionStorage.clear(); });

it('returns the same closed child with explicit complete revision instructions', async () => {
  readClosed();
  vi.mocked(returnTeamChild).mockResolvedValue({ assignment_id: 'assignment', session_id: 'child', message_id: 'new-message',
    attempt_id: 'next', sequence: 8, prior_result_message_id: 'old-result' });
  const user = userEvent.setup();
  render(<TaskSessionView projectId="project" orderId="order" profiles={[chief, worker]} reloadKey={0} />);
  await user.click(await screen.findByRole('button', { name: 'Producer · satisfied' }));
  const field = await screen.findByLabelText('Revision for Producer');
  const button = screen.getByRole('button', { name: 'Return for rework' });
  expect(button).toBeDisabled();
  await user.type(field, 'Use this complete source text and disclose its unverified adoption date.');
  await user.click(button);
  await waitFor(() => expect(returnTeamChild).toHaveBeenCalledWith('project', 'order', 'child', expect.any(String), 7,
    'old-result', 'Use this complete source text and disclose its unverified adoption date.'));
  expect(sendTeamMessage).not.toHaveBeenCalled();
  expect(await screen.findByText(/Previous results remain saved/)).toBeInTheDocument();
});

it('preserves an ambiguous rework identity and reuses it for a retry', async () => {
  readClosed();
  vi.mocked(returnTeamChild).mockRejectedValueOnce(new ApiError('Connection interrupted', null)).mockResolvedValueOnce({
    assignment_id: 'assignment', session_id: 'child', message_id: 'new', attempt_id: 'next', sequence: 8, prior_result_message_id: 'old-result',
  });
  const user = userEvent.setup();
  render(<TaskSessionView projectId="project" orderId="order" profiles={[chief, worker]} reloadKey={0} />);
  await user.click(await screen.findByRole('button', { name: 'Producer · satisfied' }));
  await user.type(await screen.findByLabelText('Revision for Producer'), 'The exact complete revision must survive a lost response.');
  await user.click(screen.getByRole('button', { name: 'Return for rework' }));
  const retry = await screen.findByRole('button', { name: 'Retry same submission' });
  await user.click(retry);
  await waitFor(() => expect(returnTeamChild).toHaveBeenCalledTimes(2));
  expect(vi.mocked(returnTeamChild).mock.calls[1]).toEqual(vi.mocked(returnTeamChild).mock.calls[0]);
});

it('does not offer in-place rework for an accepted team', async () => {
  vi.mocked(getTeamSession).mockImplementation(async (_project, _order, sessionId) => sessionId ? child : {
    ...root, session: { ...root.session, status: 'closed', work_order_status: 'done' },
  });
  const user = userEvent.setup();
  render(<TaskSessionView projectId="project" orderId="order" profiles={[chief, worker]} reloadKey={0} />);
  await user.click(await screen.findByRole('button', { name: 'Producer · satisfied' }));
  expect(await screen.findByText(/This assignment is closed/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Return for rework' })).not.toBeInTheDocument();
});

it.each([409, 422])('keeps the draft editable after a definite %s rejection', async (status) => {
  readClosed();
  vi.mocked(returnTeamChild).mockRejectedValueOnce(new ApiError('Refresh before returning this result.', status));
  const user = userEvent.setup();
  render(<TaskSessionView projectId="project" orderId="order" profiles={[chief, worker]} reloadKey={0} />);
  await user.click(await screen.findByRole('button', { name: 'Producer · satisfied' }));
  const body = 'Keep this complete feedback while the saved result is refreshed.';
  await user.type(await screen.findByLabelText('Revision for Producer'), body);
  await user.click(screen.getByRole('button', { name: 'Return for rework' }));
  // The button is enabled while the submission hash is still being prepared.
  // Wait for the API rejection before checking that its pending identity was cleared.
  await screen.findByText(/Revision was not queued/);
  await waitFor(() => expect(screen.getByRole('button', { name: 'Return for rework' })).toBeEnabled());
  expect(screen.getByLabelText('Revision for Producer')).toHaveValue(body);
  expect(screen.queryByRole('button', { name: 'Retry same submission' })).not.toBeInTheDocument();
  expect(sessionStorage.getItem('onefoundry:team-session-pending:project:order')).toBeNull();
  expect(returnTeamChild).toHaveBeenCalledTimes(1);
});

it('offers rework at an open advertising review checkpoint', async () => {
  vi.mocked(getTeamSession).mockImplementation(async (_project, _order, sessionId) => sessionId ? child : {
    ...root, session: { ...root.session, work_order_status: 'needs_review' },
  });
  const user = userEvent.setup();
  render(<TaskSessionView projectId="project" orderId="order" profiles={[chief, worker]} reloadKey={0} />);
  await user.click(await screen.findByRole('button', { name: 'Producer · satisfied' }));
  expect(await screen.findByRole('button', { name: 'Return for rework' })).toBeInTheDocument();
});

it('waits for a working sibling before offering an in-place return', async () => {
  vi.mocked(getTeamSession).mockImplementation(async (_project, _order, sessionId) => sessionId ? child : {
    ...root, children: [...root.children, { ...root.children[0], id: 'sibling', status: 'working' }],
  });
  const user = userEvent.setup();
  render(<TaskSessionView projectId="project" orderId="order" profiles={[chief, worker]} reloadKey={0} />);
  await user.click(await screen.findByRole('button', { name: 'Producer · satisfied' }));
  expect(await screen.findByText(/This assignment is closed/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Return for rework' })).not.toBeInTheDocument();
});

it('keeps the selected specialist when the task owner refresh remounts its conversation', async () => {
  readClosed();
  const user = userEvent.setup();
  const props = { projectId: 'project', orderId: 'order', profiles: [chief, worker], reloadKey: 0 };
  const first = render(<TaskSessionView {...props} />);
  await user.click(await screen.findByRole('button', { name: 'Producer · satisfied' }));
  expect(await screen.findByLabelText('Revision for Producer')).toBeInTheDocument();
  first.unmount();
  render(<TaskSessionView {...props} reloadKey={1} />);
  expect(await screen.findByLabelText('Revision for Producer')).toBeInTheDocument();
});

it('ignores a stored selection outside the current team', async () => {
  readClosed();
  sessionStorage.setItem('onefoundry:team-session-selected:project:order', 'foreign-child');
  render(<TaskSessionView projectId="project" orderId="order" profiles={[chief, worker]} reloadKey={0} />);
  expect(await screen.findByLabelText('Message to Chief of Staff')).toBeInTheDocument();
  expect(vi.mocked(getTeamSession).mock.calls.some((call) => call[2] === 'foreign-child')).toBe(false);
  expect(sessionStorage.getItem('onefoundry:team-session-selected:project:order')).toBeNull();
});

it('scopes the saved specialist selection to its project and work order', async () => {
  readClosed();
  sessionStorage.setItem('onefoundry:team-session-selected:project:order', 'child');
  render(<TaskSessionView projectId="another-project" orderId="another-order" profiles={[chief, worker]} reloadKey={0} />);
  expect(await screen.findByLabelText('Message to Chief of Staff')).toBeInTheDocument();
  expect(sessionStorage.getItem('onefoundry:team-session-selected:project:order')).toBe('child');
});
