import { afterEach, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';

import { TaskSessionView } from '../src/TaskSessionView';
import { getTeamSession, type TeamSessionView } from '../src/taskSessionApi';

vi.mock('../src/taskSessionApi', () => ({ getTeamSession: vi.fn(), sendTeamMessage: vi.fn(),
  continueTeamSession: vi.fn(), reconcileTeamActions: vi.fn(), acknowledgeUnknown: vi.fn() }));

export const receipt = { version: 1, tool_id: 'write_text_artifact_v1', call_id: 'receipt-id',
  run_id: 'attempt', status: 'done', result: { artifact_id: 'artifact-id', kind: 'markdown',
    byte_size: 13018, mime_type: 'text/markdown', sha256: 'a'.repeat(64) } };
export const saved: TeamSessionView = {
  session: { id: 'main', profile_id: 'producer', status: 'closed', execution_mode: 'team' },
  children: [], assignments: [], decision_causes: [],
  attempts: [{ id: 'attempt', session_id: 'main', client_attempt_id: null, status: 'done', reason: null,
    input_tokens: 10, output_tokens: 5, cost_usd: null,
    created_at: '2026-10-06T12:00:00Z', finished_at: '2026-10-06T12:01:00Z' }],
  messages: [{ id: 'message', session_id: 'main', client_message_id: null, sequence: 1,
    actor_id: 'profile:producer', kind: 'agent', body: JSON.stringify(receipt), created_at: '2026-10-06T12:00:05Z' }],
  tool_calls: [{ id: 'call', session_id: 'main', attempt_id: 'attempt', tool_name: 'onefoundry.role_tool',
    status: 'done', result: receipt, reason: null, created_at: '2026-10-06T12:00:03Z', finished_at: '2026-10-06T12:00:05Z' }],
};

afterEach(() => { vi.clearAllMocks(); window.sessionStorage.clear(); });

it('presents the screenshot receipt as a readable action instead of a raw agent reply', async () => {
  vi.mocked(getTeamSession).mockResolvedValue(saved);
  render(<TaskSessionView projectId="project" orderId="order" profiles={[]} reloadKey={0} />);
  expect(await screen.findByRole('button', { name: 'Show agent action activity (3)' })).toBeInTheDocument();
  expect(screen.queryByText('Save text artifact')).not.toBeInTheDocument();
  expect(screen.queryByText(JSON.stringify(receipt))).not.toBeInTheDocument();
  expect(screen.queryByText(/13,018 bytes/)).not.toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: /Turn 1/ })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Show agent action activity (3)' }));
  expect(screen.getAllByText('Save text artifact')).toHaveLength(2);
  expect(screen.getAllByText(/13,018 bytes/).length).toBeGreaterThan(0);
  expect(screen.getByRole('heading', { name: /Turn 1/ })).toBeInTheDocument();
});
it('offers copy and composer revision for emitted agent and operator messages', async () => {
  vi.mocked(getTeamSession).mockResolvedValue({ ...saved,
    session: { ...saved.session, status: 'open' },
    attempts: [], tool_calls: [],
    messages: [{ ...saved.messages[0], id: 'operator', kind: 'operator', body: 'Original request.' },
      { ...saved.messages[0], id: 'agent', sequence: 2, body: 'Saved answer.' }] });
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
  render(<TaskSessionView projectId="project" orderId="order" profiles={[]} reloadKey={0} />);
  expect(await screen.findByText('Saved answer.')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Copy agent message' }));
  expect(writeText).toHaveBeenCalledWith('Saved answer.');
  fireEvent.change(screen.getByRole('textbox', { name: 'Message to Agent' }), { target: { value: 'Unsent draft.' } });
  fireEvent.click(screen.getByRole('button', { name: 'Edit agent message' }));
  expect(screen.getByRole('textbox', { name: 'Message to Agent' })).toHaveValue('Saved answer.');
  expect(screen.getByText('Original request.')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Edit your message' }));
  expect(screen.getByRole('textbox', { name: 'Message to Agent' })).toHaveValue('Original request.');
  fireEvent.click(screen.getByRole('button', { name: 'Cancel edit' }));
  expect(screen.getByRole('textbox', { name: 'Message to Agent' })).toHaveValue('Unsent draft.');
});


it('does not let a late activity poll overwrite a newer saved snapshot', async () => {
  vi.useFakeTimers();
  let resolveOlder: (view: TeamSessionView) => void = () => {};
  const older = new Promise<TeamSessionView>((resolve) => { resolveOlder = resolve; });
  const newer = { ...saved, messages: [{ ...saved.messages[0], body: 'New recorded update.' }] };
  vi.mocked(getTeamSession).mockReturnValueOnce(older).mockResolvedValue(newer);
  try {
    render(<TaskSessionView projectId="project" orderId="order" profiles={[]} reloadKey={0} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(screen.getByText('New recorded update.')).toBeInTheDocument();
    await act(async () => { resolveOlder(saved); await older; });
    expect(screen.getByText('New recorded update.')).toBeInTheDocument();
  } finally { vi.useRealTimers(); }
});


it('hides the previous agent activity while the selected session is loading or fails', async () => {
  let rejectSelected: (error: Error) => void = () => {};
  const pending = new Promise<TeamSessionView>((_resolve, reject) => { rejectSelected = reject; });
  const root = { ...saved, messages: [{ ...saved.messages[0], body: 'Previous agent message.' }],
    children: [{ id: 'child', profile_id: 'child-profile', parent_session_id: 'main', status: 'open' as const }] };
  vi.mocked(getTeamSession).mockImplementation(async (_project, _order, id) => id ? pending : root);
  render(<TaskSessionView projectId="project" orderId="order" profiles={[]} reloadKey={0} />);
  await screen.findByText('Previous agent message.');
  fireEvent.click(screen.getByRole('button', { name: 'Agent · open' }));
  expect(screen.queryByText('Previous agent message.')).not.toBeInTheDocument();
  expect(screen.getByText('Loading session…')).toBeInTheDocument();
  await act(async () => { rejectSelected(new Error('Selected session unavailable')); });
  expect(await screen.findByRole('alert')).toHaveTextContent('Selected session unavailable');
  expect(screen.queryByText('Previous agent message.')).not.toBeInTheDocument();
});
