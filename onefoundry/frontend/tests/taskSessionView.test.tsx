import { afterEach, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import type { Profile } from '../src/api';
import { TaskSessionView } from '../src/TaskSessionView';
import { acknowledgeUnknown, continueTeamSession, getTeamSession, reconcileTeamActions, sendTeamMessage, type TeamSessionView } from '../src/taskSessionApi';

vi.mock('../src/taskSessionApi', () => ({
  getTeamSession: vi.fn(), sendTeamMessage: vi.fn(), continueTeamSession: vi.fn(), reconcileTeamActions: vi.fn(), acknowledgeUnknown: vi.fn(),
}));

it('requires an explicit reason to abandon unknown history before a separate turn', async () => {
  let acknowledged = false;
  vi.mocked(getTeamSession).mockImplementation(async () => ({ ...root, attempts: [{ ...child.attempts[0],
    session_id: 'main', status: 'unknown' as const, unknown_acknowledged: acknowledged }], tool_calls: [] }));
  vi.mocked(acknowledgeUnknown).mockImplementation(async () => { acknowledged = true; });
  const user = userEvent.setup();
  render(<TaskSessionView projectId="project" orderId="order" profiles={[chief, researcher]} reloadKey={0} />);
  const abandon = await screen.findByRole('button', { name: 'Abandon unverified result' });
  expect(abandon).toBeDisabled();
  expect(screen.queryByRole('button', { name: /Continue work/ })).not.toBeInTheDocument();
  vi.mocked(reconcileTeamActions).mockResolvedValue(0);
  await user.click(screen.getByRole('button', { name: 'Reconcile saved actions' }));
  expect(reconcileTeamActions).toHaveBeenCalledWith('project', 'order', 'main');
  await user.type(screen.getByLabelText('Reason for abandoning the unverified result'), 'Stop this uncertain result without replaying it.');
  await user.click(abandon);
  expect(acknowledgeUnknown).toHaveBeenCalledWith('project', 'order', 'main', child.attempts[0].id,
    expect.any(String), 'Stop this uncertain result without replaying it.');
  expect(await screen.findByText(/Unverified outcome abandoned/)).toBeInTheDocument();
  expect(continueTeamSession).not.toHaveBeenCalled();
});

const chief: Profile = { id: 'chief', project_id: 'project', kind: 'builtin', name: 'Chief of Staff',
  reports_to_profile_id: null, model: 'codex', model_account_id: 'account', instructions: 'Coordinate',
  memory_scope: 'project', allowed_tools: [], autonomy_level: 'manual', configured: true, permissions_active: false };
const researcher: Profile = { ...chief, id: 'researcher', name: 'Researcher', reports_to_profile_id: chief.id };
const root: TeamSessionView = {
  session: { id: 'main', profile_id: 'chief', status: 'open', execution_mode: 'team' },
  messages: [], attempts: [],
  children: [{ id: 'child', profile_id: 'researcher', parent_session_id: 'main', status: 'open' }],
  assignments: [{ id: 'assignment', child_session_id: 'child', objective: 'Review the product',
    expected_result: 'Supported findings', acceptance_check: 'Cite product evidence', status: 'blocked', result_message_id: null }],
  decision_causes: [{ id: 'cause', main_session_id: 'main', plan_revision: 1,
    kind: 'blocker', detail: 'Researcher: provider unavailable', dispatch_attempt_id: null,
    resolved_at: null, handled_at: null,
    created_at: '2026-10-04T12:01:00Z' }],
  tool_calls: [],
};
const child: TeamSessionView = {
  ...root,
  session: { id: 'child', profile_id: 'researcher', status: 'open', execution_mode: 'team' },
  messages: [{ id: 'message', session_id: 'child', client_message_id: null, sequence: 1,
    actor_id: 'profile:researcher', kind: 'agent', body: 'I found product evidence.', created_at: '2026-10-04T12:00:00Z' }],
  attempts: [{ id: 'attempt', session_id: 'child', client_attempt_id: null, status: 'failed',
    reason: 'provider_unavailable', input_tokens: null, output_tokens: null, cost_usd: null,
    created_at: '2026-10-04T12:00:00Z', finished_at: '2026-10-04T12:01:00Z' }],
  tool_calls: [{ id: 'tool', session_id: 'child', attempt_id: 'attempt',
    tool_name: 'onefoundry.publish_update', status: 'done', result: { message_id: 'message' },
    reason: null, created_at: '2026-10-04T12:00:01Z', finished_at: '2026-10-04T12:00:02Z' }],
};

afterEach(() => { vi.clearAllMocks(); window.sessionStorage.clear(); });

it('clears a transient session load error after the next successful poll', async () => {
  vi.useFakeTimers();
  vi.mocked(getTeamSession).mockRejectedValueOnce(new Error('Temporary read failure')).mockResolvedValue(root);
  try {
    render(<TaskSessionView projectId="project" orderId="order" profiles={[chief]} reloadKey={0} />);
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByRole('alert')).toHaveTextContent('Temporary read failure');
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByText('Chief of Staff', { selector: 'strong' })).toBeInTheDocument();
  } finally { vi.useRealTimers(); }
});

it('explains recent history and sends the current sequence for a long task', async () => {
  vi.mocked(getTeamSession).mockResolvedValue({ ...root,
    messages: [{ ...child.messages[0], session_id: 'main', sequence: 501 }],
    attempts: [{ ...child.attempts[0], session_id: 'main', status: 'done', reason: null }],
    history_omitted: { messages: 1, attempts: 1, decision_causes: 0, tool_calls: 0 },
  });
  vi.mocked(sendTeamMessage).mockResolvedValue({ session_id: 'main', message_id: 'saved', attempt_id: 'next', sequence: 502, state: 'queued' });
  const user = userEvent.setup();
  render(<TaskSessionView projectId="project" orderId="order" profiles={[chief]} reloadKey={0} />);
  expect(await screen.findByText(/2 earlier entries remain saved/)).toBeInTheDocument();
  expect(screen.getByText(/Visible turn usage/)).toBeInTheDocument();
  await user.type(screen.getByLabelText('Message to Chief of Staff'), 'Continue the long goal.');
  await user.click(screen.getByRole('button', { name: 'Send to agent' }));
  await waitFor(() => expect(sendTeamMessage).toHaveBeenCalledWith('project', 'order', 'main', expect.any(String), 501, 'Continue the long goal.'));
});
it('keeps a long team direction when the send is rejected', async () => {
  vi.mocked(getTeamSession).mockResolvedValue({ ...root, children: [] });
  vi.mocked(sendTeamMessage).mockRejectedValue(new Error('Configured context budget exceeded'));
  const rendered = render(<TaskSessionView projectId="project" orderId="order" profiles={[chief]} reloadKey={0} />);
  const composer = await screen.findByLabelText('Message to Chief of Staff');
  const body = 'Read full direction. ' + 'source line '.repeat(1500) + 'done';
  fireEvent.change(composer, { target: { value: body } });
  await userEvent.setup().click(screen.getByRole('button', { name: 'Send to agent' }));
  await waitFor(() => expect(sendTeamMessage).toHaveBeenCalledWith('project', 'order', 'main', expect.any(String), 0, body));
  expect(composer).toHaveValue(body);
  expect(await screen.findByText(/Configured context budget exceeded/)).toBeInTheDocument();
  rendered.rerender(<TaskSessionView projectId="project" orderId="order" profiles={[chief]} reloadKey={1} />);
  await waitFor(() => expect(vi.mocked(getTeamSession).mock.calls.length).toBeGreaterThan(1));
  expect(screen.getByRole('alert')).toHaveTextContent('Configured context budget exceeded');
  vi.mocked(getTeamSession).mockRejectedValueOnce(new Error('Saved state unavailable'));
  rendered.rerender(<TaskSessionView projectId="project" orderId="order" profiles={[chief]} reloadKey={2} />);
  await screen.findByText(/Saved-state refresh failed: Saved state unavailable/);
  expect(screen.getAllByRole('alert')).toHaveLength(2);
  expect(screen.getByText(/Configured context budget exceeded/)).toBeInTheDocument();
  expect(composer).toHaveValue(body);
});

it.each([
  ['local_recording', 'Waiting for your local recording.', true],
  ['recording_unconfirmed', 'The recording storage outcome is unconfirmed.', true],
  ['recording_ready', 'Video saved. Open Results to verify its playback', false],
] as const)('explains %s without confusing it with a team limit', async (reason, guidance, disabled) => {
  vi.mocked(getTeamSession).mockResolvedValue({ ...root, session: {
    ...root.session, status: 'waiting_for_operator', waiting_reason: reason,
  } });
  render(<TaskSessionView projectId="project" orderId="order" profiles={[chief, researcher]} reloadKey={0} />);
  expect(await screen.findByText((content) => content.startsWith(guidance))).toBeInTheDocument();
  expect(screen.queryByText(/paused at an automatic safeguard/)).not.toBeInTheDocument();
  const continuation = screen.queryByRole('button', { name: /(?:Start|Continue) work with Chief of Staff/ });
  if (disabled) expect(continuation).not.toBeInTheDocument();
  else expect(continuation).toBeEnabled();
  expect(continueTeamSession).not.toHaveBeenCalled();
});

it('attributes QA feedback and presents readable saved results', async () => {
  vi.mocked(getTeamSession).mockResolvedValue({ ...root, session: { ...root.session, work_order_status: 'needs_review' },
    messages: [{ id: 'qa-feedback', session_id: 'main', client_message_id: null, sequence: 1,
      actor_id: 'profile:qa', kind: 'agent', body: JSON.stringify({ verdict: 'needs_rework', feedback: 'Remove the unsupported claim.', reviewed_artifacts: [] }), created_at: '2026-10-05T12:00:00Z' }] });
  render(<TaskSessionView projectId="project" orderId="order" profiles={[chief, researcher, { ...chief, id: 'qa', name: 'QA' }]} reloadKey={0} />);
  expect(await screen.findByText(/QA needs rework/)).toHaveTextContent('Remove the unsupported claim.');
  expect(screen.getByText('QA')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /Continue work/ })).not.toBeInTheDocument();
});

it('reconciles an interrupted saved action on a closed assignment without a model turn', async () => {
  const uncertain = { ...child, session: { ...child.session, status: 'closed' as const },
    tool_calls: [{ ...child.tool_calls[0], status: 'started' as const, result: null, finished_at: null }] };
  vi.mocked(getTeamSession).mockImplementation(async (_project, _order, sessionId) => sessionId ? uncertain : root);
  vi.mocked(reconcileTeamActions).mockResolvedValue(1);
  render(<TaskSessionView projectId="project" orderId="order" profiles={[chief, researcher]} reloadKey={0} />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: 'Researcher · blocked' }));
  await user.click(await screen.findByRole('button', { name: 'Reconcile saved actions' }));
  expect(reconcileTeamActions).toHaveBeenCalledWith('project', 'order', 'child');
  expect(await screen.findByText('1 saved action receipts reconciled. No agent turn was launched.')).toBeInTheDocument();
  expect(continueTeamSession).not.toHaveBeenCalled();
});

it('shows a child session without launching either agent and continues after a failed attempt', async () => {
  vi.mocked(getTeamSession).mockImplementation(async (_project, _order, sessionId) => sessionId ? child : root);
  vi.mocked(continueTeamSession).mockResolvedValue({ session_id: 'child', message_id: null,
    attempt_id: 'next', sequence: 1, state: 'queued' });
  const user = userEvent.setup();
  render(<TaskSessionView projectId="project" orderId="order" profiles={[chief, researcher]} reloadKey={0} />);
  await user.click(await screen.findByRole('button', { name: 'Researcher · blocked' }));
  expect(await screen.findByText('I found product evidence.')).toBeInTheDocument();
  expect(screen.queryByText('Public update')).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: /Show agent action activity/ }));
  expect(screen.getByText('Public update')).toBeInTheDocument();
  expect(screen.getAllByText('Review the product')).toHaveLength(2);
  expect(screen.getByText('Researcher: provider unavailable')).toBeInTheDocument();
  expect(screen.getAllByText(/USD cost unknown/).length).toBeGreaterThan(0);
  expect(continueTeamSession).not.toHaveBeenCalled();
  expect(sendTeamMessage).not.toHaveBeenCalled();
  await user.click(screen.getByRole('button', { name: 'Continue work with Researcher' }));
  await waitFor(() => expect(continueTeamSession).toHaveBeenCalledWith('project', 'order', 'child', expect.any(String)));
  expect(await screen.findByText('Continuation queued for this agent.')).toBeInTheDocument();
});

it('saves a child message while its current turn is working without another immediate turn', async () => {
  const running = { ...child, session: { ...child.session, status: 'working' as const },
    attempts: [{ ...child.attempts[0], status: 'running' as const, reason: null, finished_at: null }] };
  vi.mocked(getTeamSession).mockImplementation(async (_project, _order, sessionId) => sessionId ? running : root);
  vi.mocked(sendTeamMessage).mockResolvedValue({ session_id: 'child', message_id: 'saved',
    attempt_id: null, sequence: 2, state: 'coalesced' });
  const user = userEvent.setup();
  render(<TaskSessionView projectId="project" orderId="order" profiles={[chief, researcher]} reloadKey={0} />);
  await user.click(await screen.findByRole('button', { name: 'Researcher · blocked' }));
  await screen.findByText('I found product evidence.');
  await user.type(screen.getByLabelText('Message to Researcher'), 'Please add the source.');
  await user.click(screen.getByRole('button', { name: 'Send to agent' }));
  await waitFor(() => expect(sendTeamMessage).toHaveBeenCalledWith('project', 'order', 'child',
    expect.any(String), 1, 'Please add the source.'));
  expect(await screen.findByText('Message saved. The agent will receive it on its next turn.')).toBeInTheDocument();
  expect(continueTeamSession).not.toHaveBeenCalled();
});

it('explains a team-limit pause, a scheduled retry, and the provider each turn used', async () => {
  const paused: TeamSessionView = { ...root, session: { ...root.session, status: 'waiting_for_operator' },
    attempts: [{ id: 'done', session_id: 'main', client_attempt_id: null, status: 'done', reason: null,
      input_tokens: 10, output_tokens: 5, cost_usd: null, provider: 'ollama', model_id: 'qwen-test',
      created_at: '2026-10-04T12:00:00Z', finished_at: '2026-10-04T12:00:05Z' }] };
  const retrying: TeamSessionView = { ...child,
    attempts: [{ ...child.attempts[0], reason: 'provider_rate_limited' },
      { ...child.attempts[0], id: 'retry', status: 'queued', reason: null, finished_at: null,
        retry_of_attempt_id: 'attempt' }] };
  vi.mocked(getTeamSession).mockImplementation(async (_project, _order, sessionId) => sessionId ? retrying : paused);
  const user = userEvent.setup();
  render(<TaskSessionView projectId="project" orderId="order" profiles={[chief, researcher]} reloadKey={0} />);
  expect(await screen.findByText(/paused at an automatic safeguard/)).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: /Show agent action activity/ }));
  expect(screen.getByText(/ollama qwen-test/)).toBeInTheDocument();
  await user.click(await screen.findByRole('button', { name: 'Researcher · blocked' }));
  expect(await screen.findByText(/will retry it once shortly/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Continue work with Researcher' })).not.toBeInTheDocument();
  expect(continueTeamSession).not.toHaveBeenCalled();
});


it('keeps a rejected work order visibly failed even though its result receipt was saved', async () => {
  vi.mocked(getTeamSession).mockResolvedValue({ ...root,
    children: [{ ...root.children[0], work_order_status: 'failed' }],
    assignments: [{ ...root.assignments[0], status: 'satisfied', result_message_id: 'saved-result' }],
  });
  render(<TaskSessionView projectId="project" orderId="order" profiles={[chief, researcher]} reloadKey={0} />);
  expect(await screen.findByRole('button', { name: 'Researcher · work order failed' })).toBeInTheDocument();
  expect(screen.getByText('Result saved')).toBeInTheDocument();
});

it('labels preserved legacy child history and never offers its execution controls', async () => {
  const legacy = { ...child, session: { ...child.session, execution_mode: 'legacy' as const } };
  vi.mocked(getTeamSession).mockImplementation(async (_project, _order, sessionId) => sessionId ? legacy :
    { ...root, children: [{ ...root.children[0], execution_mode: 'legacy' as const }] });
  render(<TaskSessionView projectId="project" orderId="order" profiles={[chief, researcher]} reloadKey={0} />);
  await userEvent.setup().click(await screen.findByRole('button', { name: 'Researcher · preserved legacy draft' }));
  await screen.findByText('I found product evidence.');
  expect(screen.queryByRole('button', { name: /Continue work/ })).not.toBeInTheDocument();
  expect(sendTeamMessage).not.toHaveBeenCalled();
  expect(continueTeamSession).not.toHaveBeenCalled();
});
