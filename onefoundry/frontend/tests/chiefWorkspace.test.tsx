import { afterEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ChiefWorkspace } from '../src/ChiefWorkspace';
import { publicMessage } from '../src/ChiefConversationText';
import type { Project } from '../src/api';

const project = { id: 'project-1', name: 'Studio' } as Project;
const row = { id: 'order-1', title: 'Product brief', status: 'failed', version: 3, profile_id: 'chief-1',
  profile_name: 'Chief of Staff', archived: false, paused: false, controls_version: 1, canonical_chief: true,
  completion_policy: 'chief_verified', state: 'Stopped', detail: 'Work stopped. Message Chief here to review the saved work and continue.' };
const view = { session: { id: 'session-1', profile_id: 'chief-1', status: 'open', execution_mode: 'team' },
  messages: [{ id: 'message-1', sequence: 1, kind: 'operator', actor_id: 'operator', session_id: 'session-1', body: 'Create the brief.' }],
  attempts: [], children: [], assignments: [], decision_causes: [], dependencies: [], tool_calls: [] };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
afterEach(() => { vi.unstubAllGlobals(); window.sessionStorage.clear(); });
function renderWorkspace(initialOrderId: string | null = null) {
  return render(<ChiefWorkspace project={project} profiles={[]} initialOrderId={initialOrderId}
    onSelect={vi.fn()} onMenu={vi.fn()} onNavigate={vi.fn()} />);
}
it('starts from one composer without creating a draft on open', async () => {
  const requests: Record<string, unknown>[] = [];
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/chief-workspace/messages')) {
      requests.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
      return json({ order_id: row.id, session_id: 'session-1', message_id: 'saved-1', attempt_id: 'attempt-1', sequence: 1, disposition: 'queued', detail: null }, 202);
    }
    if (path.endsWith('/chief-workspace')) return json(requests.length ? [{ ...row, state: 'Queued' }] : []);
    if (path.endsWith('/team-session')) return json(view);
    if (path.endsWith('/work-orders')) return json([]);
    return json([]);
  }));
  const user = userEvent.setup(); renderWorkspace();
  expect(screen.getAllByRole('textbox', { name: 'Message Chief' })).toHaveLength(1);
  expect(requests).toHaveLength(0);
  await user.type(screen.getByRole('textbox', { name: 'Message Chief' }), 'Make the product brief.');
  await user.click(screen.getByRole('button', { name: /Send/ }));
  await waitFor(() => expect(requests).toHaveLength(1));
  expect(requests[0]?.order_id).toBeUndefined();
  expect(requests[0]?.body).toBe('Make the product brief.');
});
it('keeps a long draft intact when the configured provider budget rejects it', async () => {
  const longText = 'Complete objective. ' + 'evidence line '.repeat(1500) + 'done';
  let sent = '';
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/chief-workspace/messages')) {
      sent = (JSON.parse(String(init?.body)) as { body: string }).body;
      return json({ detail: 'operator message exceeds configured context budget' }, 409);
    }
    if (path.endsWith('/chief-workspace')) return json([]);
    return json([]);
  }));
  renderWorkspace();
  const composer = await screen.findByRole('textbox', { name: 'Message Chief' });
  fireEvent.change(composer, { target: { value: longText } });
  expect(composer).toHaveValue(longText);
  await userEvent.setup().click(screen.getByRole('button', { name: /Send/ }));
  await waitFor(() => expect(sent).toBe(longText));
  expect(composer).toHaveValue(longText);
  expect(screen.getByRole('alert')).toHaveTextContent('configured context budget');
});
it('copies and revises saved operator and agent text without changing the transcript or sending', async () => {
  const send = vi.fn();
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.includes('/resolve/')) return json({ order_id: row.id, agent_session_id: 'session-1' });
    if (path.endsWith('/chief-workspace')) return json([row]);
    if (path.endsWith('/team-session')) return json({ ...view, messages: [view.messages[0],
      { ...view.messages[0], id: 'agent-1', sequence: 2, kind: 'agent', actor_id: 'profile:chief-1', body: 'Saved answer.' }] });
    if (path.endsWith('/chief-workspace/messages')) { send(); return json({}); }
    return json([]);
  }));
  const writeText = vi.fn().mockResolvedValue(undefined);
  const user = userEvent.setup();
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
  renderWorkspace(row.id);
  await screen.findAllByText('Saved answer.');
  await user.click(screen.getByRole('button', { name: 'Copy agent message' }));
  expect(writeText).toHaveBeenCalledWith('Saved answer.');
  await user.type(screen.getByRole('textbox', { name: 'Message Chief' }), 'Unsent draft.');
  await user.click(screen.getByRole('button', { name: 'Edit agent message' }));
  expect(screen.getByRole('textbox', { name: 'Message Chief' })).toHaveValue('Saved answer.');
  expect(screen.getAllByText('Create the brief.').length).toBeGreaterThan(0);
  expect(screen.getAllByText('Saved answer.').length).toBeGreaterThan(0);
  await user.click(screen.getByRole('button', { name: 'Edit your message' }));
  expect(screen.getByRole('textbox', { name: 'Message Chief' })).toHaveValue('Create the brief.');
  await user.click(screen.getByRole('button', { name: 'Cancel edit' }));
  expect(screen.getByRole('textbox', { name: 'Message Chief' })).toHaveValue('Unsent draft.');
  expect(send).not.toHaveBeenCalled();
});
it('displays an operator JSON message literally for copy and revision', async () => {
  const body = '{"summary":"Keep the complete operator request","reply":"Literal JSON"}';
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.includes('/resolve/')) return json({ order_id: row.id, agent_session_id: 'session-1' });
    if (path.endsWith('/chief-workspace')) return json([row]);
    if (path.endsWith('/team-session')) return json({ ...view, messages: [{ ...view.messages[0], body }] });
    return json([]);
  }));
  const user = userEvent.setup(); const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
  renderWorkspace(row.id);
  expect((await screen.findAllByText(body)).length).toBeGreaterThan(0);
  await user.click(screen.getByRole('button', { name: 'Copy your message' }));
  expect(writeText).toHaveBeenCalledWith(body);
  await user.click(screen.getByRole('button', { name: 'Edit your message' }));
  expect(screen.getByRole('textbox', { name: 'Message Chief' })).toHaveValue(body);
  expect(screen.getAllByText(body).length).toBeGreaterThan(0);
});
it('keeps saved system, tool receipt, and unreadable agent data out of the main conversation', async () => {
  const receipt = '{"tool_id":"write_text_artifact_v1","status":"done"}';
  const unknown = '{"internal_field":"not a public reply"}';
  const messages = [view.messages[0],
    { ...view.messages[0], id: 'system', sequence: 2, kind: 'system', body: 'Verbose system context.' },
    { ...view.messages[0], id: 'receipt', sequence: 3, kind: 'agent', body: receipt },
    { ...view.messages[0], id: 'unknown', sequence: 4, kind: 'agent', body: unknown },
    { ...view.messages[0], id: 'malformed', sequence: 5, kind: 'agent', body: '{broken' },
    { ...view.messages[0], id: 'summary', sequence: 6, kind: 'agent', body: '{"summary":"Usable public update."}' },
    { ...view.messages[0], id: 'scalar', sequence: 7, kind: 'agent', body: '4' },
    { ...view.messages[0], id: 'markdown', sequence: 8, kind: 'agent', body: '[Plan] Checking sources' }];
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.includes('/resolve/')) return json({ order_id: row.id, agent_session_id: 'session-1' });
    if (path.endsWith('/chief-workspace')) return json([row]);
    if (path.endsWith('/team-session')) return json({ ...view, messages });
    return json([]);
  }));
  const user = userEvent.setup(); renderWorkspace(row.id);
  const publicUpdates = await screen.findAllByText('Usable public update.');
  expect(publicUpdates.filter((node) => !node.closest('details'))).toHaveLength(1);
  expect(screen.getAllByText('4').length).toBeGreaterThan(0);
  expect(screen.getAllByText('[Plan] Checking sources').length).toBeGreaterThan(0);
  expect(screen.queryByText('Verbose system context.')).not.toBeInTheDocument();
  expect(screen.queryByText(receipt)).not.toBeInTheDocument();
  expect(screen.queryByText(unknown)).not.toBeInTheDocument();
  expect(screen.queryByText('{broken')).not.toBeInTheDocument();
  await user.click(screen.getByText('Details & earlier attempts'));
  await user.click(screen.getByRole('button', { name: /Show agent action activity/ }));
  expect(screen.getByText('Verbose system context.')).toBeInTheDocument();
  expect(screen.getByText('Save text artifact')).toBeInTheDocument();
});
it('continues a stopped job from the same chat and shows an unresolved disposition', async () => {
  let sent: Record<string, unknown> | null = null;
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.includes('/resolve/')) return json({ order_id: row.id, agent_session_id: 'session-1' });
    if (path.endsWith('/chief-workspace/messages')) {
      sent = JSON.parse(String(init?.body)) as Record<string, unknown>;
      return json({ order_id: row.id, session_id: 'session-1', message_id: 'saved-1', attempt_id: 'attempt-1', sequence: 2,
        disposition: 'discussion', detail: 'Chief can inspect; uncertain effects remain held.' }, 202);
    }
    if (path.endsWith('/chief-workspace')) return json([row]);
    if (path.endsWith('/team-session')) return json(view);
    return json([]);
  }));
  const user = userEvent.setup(); renderWorkspace(row.id);
  await screen.findAllByText('Create the brief.');
  expect(screen.getByRole('status')).toHaveTextContent('Message Chief here');
  expect(screen.getByRole('textbox', { name: 'Message Chief' })).toBeEnabled();
  await user.type(screen.getByRole('textbox', { name: 'Message Chief' }), 'Continue from where we stopped.');
  await user.click(screen.getByRole('button', { name: /Send/ }));
  await screen.findByText('Chief can inspect; uncertain effects remain held.');
  expect(sent).toMatchObject({ order_id: row.id, expected_version: 3, expected_sequence: 1 });
});
it('keeps the same submission identity after an unknown network result', async () => {
  const requests: Record<string, unknown>[] = [];
  let networkDown = true;
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/chief-workspace/messages')) {
      requests.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
      if (networkDown) throw new Error('Connection lost');
      return json({ order_id: row.id, session_id: 'session-1', message_id: 'saved-1', attempt_id: 'attempt-1', sequence: 1, disposition: 'queued', detail: null }, 202);
    }
    if (path.includes('/chief-workspace/messages/')) return json({ detail: 'Not recorded' }, 404);
    if (path.endsWith('/chief-workspace')) return json([]);
    if (path.endsWith('/team-session')) return json(view);
    return json([]);
  }));
  const user = userEvent.setup(); renderWorkspace();
  await user.type(screen.getByRole('textbox', { name: 'Message Chief' }), 'Make one brief.');
  await user.click(screen.getByRole('button', { name: /Send/ }));
  await screen.findByText(/Keep this text/);
  networkDown = false;
  await user.click(screen.getByRole('button', { name: /Send/ }));
  await waitFor(() => expect(requests).toHaveLength(2));
  expect(requests[0]).toEqual(requests[1]);
});
it('opens agent details without a second composer or specialist management', async () => {
  const child = { id: 'child-1', profile_id: 'research-1', work_order_id: 'child-order', status: 'closed', parent_session_id: 'session-1' };
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.includes('/resolve/')) return json({ order_id: row.id, agent_session_id: 'session-1' });
    if (path.endsWith('/chief-workspace')) return json([row]);
    if (path.endsWith('/team-session')) return json({ ...view, children: [child], assignments: [{ id: 'assignment-1', child_session_id: child.id,
      objective: 'Verify the source brief.', expected_result: 'Confirmed findings', acceptance_check: 'Cite sources', status: 'satisfied' }] });
    if (path.endsWith('/sessions/child-1')) return json({ ...view, session: { ...view.session, id: child.id }, messages: [{ ...view.messages[0], body: 'Confirmed source findings.', kind: 'agent' }] });
    return json([]);
  }));
  const user = userEvent.setup(); renderWorkspace(row.id);
  await user.click(await screen.findByText('Past agent work (1)'));
  await user.click(screen.getByRole('button', { name: 'Open Agent: Verify the source brief. (Past)' }));
  const panel = screen.getByRole('dialog', { name: 'Agent' });
  expect(within(panel).getByText('Verify the source brief.')).toBeInTheDocument();
  expect(screen.getAllByRole('textbox', { name: 'Message Chief' })).toHaveLength(1);
  expect(within(panel).queryByRole('textbox')).not.toBeInTheDocument();
  await user.click(within(panel).getByRole('tab', { name: 'Conversation' }));
  expect(await within(panel).findByText('Confirmed source findings.')).toBeInTheDocument();
  await user.keyboard('{Escape}');
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});
it('presents saved structured agent findings as readable public text', () => {
  expect(publicMessage(JSON.stringify({ verdict: 'needs_rework', feedback: 'Revise the unsupported claim.', reviewed_artifacts: [{ sha256: 'private-receipt' }] })))
    .toBe('Changes requested\nRevise the unsupported claim.');
  expect(publicMessage(JSON.stringify({ summary: 'Verified product brief', artifact: { artifact_id: 'internal' } }))).toBe('Verified product brief');
});

it('opens a Chief artifact reference from its exact saved root and clears the preview on conversation change', async () => {
  const sha = 'd'.repeat(64);
  const artifactId = '17cf947d-eabd-4f2a-9382-24eb760dc65d';
  const nextId = 'a1f083c3-6db5-4ce7-b6b5-b1c9b6f75f48';
  const final = { ...view.messages[0], id: 'final-message', sequence: 2, kind: 'agent', actor_id: 'profile:chief-1',
    body: `Final notes: [final index](artifact:${sha})` };
  const artifact = { id: artifactId, run_id: 'run-1', profile_id: 'research-1', profile_name: 'Researcher',
    kind: 'markdown', status: 'ready', sha256: sha };
  const saved = { id: artifactId, kind: 'markdown', mime_type: 'text/markdown', sha256: sha,
    byte_size: 22, status: 'ready', created_at: '2026-10-18T12:00:00Z', finished_at: '2026-10-18T12:01:00Z' };
  const fetch = vi.fn(async (path: string) => {
    if (path.includes('/resolve/')) return json({ order_id: row.id, agent_session_id: null });
    if (path.endsWith('/chief-workspace')) return json([row, { ...row, id: 'order-2', title: 'Next task' }]);
    if (path.endsWith('/work-orders/order-1/team-session')) return json({ ...view, messages: [...view.messages, final] });
    if (path.endsWith('/work-orders/order-2/team-session')) return json({ ...view, messages: [...view.messages, final] });
    if (path.endsWith('/work-orders/order-1/team-session/artifacts')) return json([artifact]);
    if (path.endsWith('/work-orders/order-2/team-session/artifacts')) return json([{ ...artifact, id: nextId, run_id: 'run-2' }]);
    if (path.endsWith('/runs/run-1/workspace-artifacts')) return json([saved]);
    if (path.endsWith(`/runs/run-1/workspace-artifacts/${artifactId}`)) return json({ ...saved, content: 'Exact final index' });
    return json([]);
  });
  vi.stubGlobal('fetch', fetch);
  const user = userEvent.setup(); renderWorkspace(row.id);
  await user.click(await screen.findByRole('button', { name: 'Open saved file final index' }));
  expect(await screen.findByText('Exact final index')).toBeInTheDocument();
  expect(fetch.mock.calls.some(([path]) => path.endsWith(`/runs/run-1/workspace-artifacts/${artifactId}`))).toBe(true);
  await user.click(screen.getByTitle('Next task'));
  await screen.findByRole('heading', { name: 'Next task' });
  await screen.findByRole('button', { name: 'Open saved file final index' });
  expect(screen.queryByRole('region', { name: 'Saved file preview' })).not.toBeInTheDocument();
});

it('shows the saved team blocker and opens its exact affected child', async () => {
  const child = { id: 'child-unknown', profile_id: 'research-1', work_order_id: 'child-order', status: 'closed', parent_session_id: 'session-1' };
  const progress = { counts: { active_attempts: 0, queued_attempts: 0, unfinished_assignments: 0,
    blocked_assignments: 0, ready_artifacts: 2 }, paused: true,
    blocker: { kind: 'unknown_attempt', detail: 'Researcher has an unverified outcome.', session_id: child.id, attempt_id: 'attempt-unknown' },
    next_action: { kind: 'open_agent', label: 'Review Researcher outcome', session_id: child.id } };
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.includes('/resolve/')) return json({ order_id: row.id, agent_session_id: null });
    if (path.endsWith('/chief-workspace')) return json([{ ...row, state: 'Paused', paused: true, status: 'running' }]);
    if (path.endsWith('/chief-workspace/order-1/progress')) return json(progress);
    if (path.endsWith('/team-session')) return json({ ...view, children: [child] });
    if (path.endsWith('/sessions/child-unknown')) return json({ ...view, session: { ...view.session, id: child.id },
      attempts: [{ id: 'attempt-unknown', session_id: child.id, status: 'unknown', unknown_acknowledged: false,
        reason: 'runner_interrupted', created_at: '2026-10-18T12:00:00Z', finished_at: '2026-10-18T12:01:00Z' }] });
    return json([]);
  }));
  renderWorkspace(row.id);
  expect(await screen.findByRole('region', { name: 'Current work' })).toHaveTextContent('Researcher has an unverified outcome.');
  expect(screen.getByRole('region', { name: 'Current work' })).toHaveTextContent('Paused');
  expect(within(screen.getByRole('region', { name: 'Current work' })).queryByRole('button', { name: 'Resume work' })).not.toBeInTheDocument();
  await userEvent.setup().click(screen.getByRole('button', { name: 'Review Researcher outcome' }));
  expect(await screen.findByRole('dialog', { name: 'Agent' })).toBeInTheDocument();
});

it('opens blocked QA feedback directly while ordinary agent navigation still starts on Context', async () => {
  const child = { id: 'qa-child', profile_id: 'qa-profile', work_order_id: 'qa-order', status: 'closed', parent_session_id: 'session-1' };
  const feedback = 'Remove the unsupported claim and add the missing legal and card details.';
  const report = JSON.stringify({ verdict: 'needs_rework', feedback, reviewed_artifacts: [{ sha256: 'internal-receipt' }] });
  const progress = { counts: { active_attempts: 0, queued_attempts: 0, unfinished_assignments: 1,
    blocked_assignments: 1, ready_artifacts: 0 }, paused: true,
    blocker: { kind: 'blocked_assignment', detail: 'A delegated assignment is blocked.', session_id: child.id },
    next_action: { kind: 'open_agent', label: 'Review blocked agent', session_id: child.id } };
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.includes('/resolve/')) return json({ order_id: row.id, agent_session_id: null });
    if (path.endsWith('/chief-workspace')) return json([{ ...row, state: 'Paused', paused: true, status: 'running' }]);
    if (path.endsWith('/chief-workspace/order-1/progress')) return json(progress);
    if (path.endsWith('/team-session')) return json({ ...view, children: [child], assignments: [{ id: 'qa-assignment', child_session_id: child.id,
      objective: 'Review the research package.', expected_result: 'QA verdict', acceptance_check: 'Verify claims', status: 'blocked' }] });
    if (path.endsWith('/sessions/qa-child')) return json({ ...view, session: { ...view.session, id: child.id }, messages: [
      { ...view.messages[0], id: 'qa-feedback', kind: 'agent', actor_id: 'profile:qa-profile', body: report }], attempts: [] });
    return json([]);
  }));
  const user = userEvent.setup(); renderWorkspace(row.id);
  await user.click(await screen.findByRole('button', { name: 'Review blocked agent' }));
  let panel = screen.getByRole('dialog', { name: 'Agent' });
  expect(within(panel).getByRole('tab', { name: 'Conversation' })).toHaveAttribute('aria-selected', 'true');
  const review = await within(panel).findByRole('region', { name: 'Review feedback' });
  expect(review).toHaveTextContent('Changes requested');
  expect(review).toHaveTextContent(feedback);
  expect(review).not.toHaveTextContent('internal-receipt');
  expect(within(panel).getAllByText(new RegExp(feedback))).toHaveLength(1);
  await user.click(within(panel).getByRole('tab', { name: 'Context' }));
  expect(within(panel).getByText('Review the research package.')).toBeInTheDocument();
  await user.click(within(panel).getByRole('button', { name: '← Chief' }));
  await user.click(screen.getByRole('button', { name: 'Open Agent: Review the research package. (Blocked)' }));
  panel = screen.getByRole('dialog', { name: 'Agent' });
  expect(within(panel).getByRole('tab', { name: 'Context' })).toHaveAttribute('aria-selected', 'true');
});

it('shows a direct resume control for paused idle work with saved files', async () => {
  const progress = { counts: { active_attempts: 0, queued_attempts: 0, unfinished_assignments: 0,
    blocked_assignments: 0, ready_artifacts: 2 }, paused: true, blocker: null,
    next_action: { kind: 'review_deliverables', label: 'Review saved deliverables' } };
  const controls = vi.fn();
  let confirmControl: ((response: Response) => void) | undefined;
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.includes('/resolve/')) return json({ order_id: row.id, agent_session_id: null });
    if (path.endsWith('/chief-workspace')) return json([{ ...row, paused: true, state: 'Paused', status: 'running' }]);
    if (path.endsWith('/chief-workspace/order-1/progress')) return json(progress);
    if (path.endsWith('/team-session')) return json(view);
    if (path.endsWith('/chief-workspace/order-1/controls')) {
      controls(JSON.parse(String(init?.body)));
      return await new Promise<Response>((resolve) => { confirmControl = resolve; });
    }
    return json([]);
  }));
  renderWorkspace(row.id);
  const card = await screen.findByRole('region', { name: 'Current work' });
  expect(card).toHaveTextContent('No agent work is running or queued.');
  expect(card).toHaveTextContent('Saved files are ready to review.');
  expect(card).not.toHaveTextContent('Chief is reviewing');
  const actions = within(card).getByRole('group', { name: 'Current work actions' });
  expect(within(actions).getAllByRole('button')).toHaveLength(2);
  expect(within(actions).getByRole('button', { name: 'Resume work' })).toHaveClass('chief-progress-resume');
  expect(within(actions).getByRole('button', { name: 'Review saved deliverables' })).toHaveClass('chief-progress-review');
  await userEvent.setup().click(within(card).getByRole('button', { name: 'Resume work' }));
  await waitFor(() => expect(controls).toHaveBeenCalledOnce());
  expect(within(card).getByRole('button', { name: 'Resume work' })).toBeDisabled();
  expect(controls.mock.calls[0]?.[0]).toMatchObject({ action: 'resume', expected_version: 3,
    expected_controls_version: 1 });
  confirmControl?.(json({ order_id: row.id, action: 'resume', controls_version: 2 }));
  await waitFor(() => expect(within(card).getByRole('button', { name: 'Resume work' })).toBeEnabled());
});

it('does not imply an agent is reviewing idle saved files', async () => {
  const progress = { counts: { active_attempts: 0, queued_attempts: 0, unfinished_assignments: 0,
    blocked_assignments: 0, ready_artifacts: 2 }, paused: false, blocker: null,
    next_action: { kind: 'review_deliverables', label: 'Review saved deliverables' } };
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.includes('/resolve/')) return json({ order_id: row.id, agent_session_id: null });
    if (path.endsWith('/chief-workspace')) return json([{ ...row, status: 'running', state: 'Idle' }]);
    if (path.endsWith('/chief-workspace/order-1/progress')) return json(progress);
    if (path.endsWith('/team-session')) return json(view);
    return json([]);
  }));
  renderWorkspace(row.id);
  const card = await screen.findByRole('region', { name: 'Current work' });
  expect(card).toHaveTextContent('No agent work is running or queued.');
  expect(card).toHaveTextContent('Saved files are ready to review.');
  expect(within(card).queryByRole('button', { name: 'Resume work' })).not.toBeInTheDocument();
});

it.each([
  { name: 'unfinished assignment', queued: 0,
    blocker: { kind: 'unfinished_assignment', detail: 'A delegated assignment is unfinished.', session_id: 'child-1' },
    next_action: { kind: 'open_agent', label: 'Review unfinished agent', session_id: 'child-1' } },
  { name: 'queued work', queued: 1, blocker: null,
    next_action: { kind: 'wait', label: 'Work is in progress' } },
])('offers Resume work for paused $name', async ({ queued, blocker, next_action }) => {
  const progress = { counts: { active_attempts: 0, queued_attempts: queued, unfinished_assignments: 1,
    blocked_assignments: 0, ready_artifacts: 0 }, paused: true, blocker, next_action };
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.includes('/resolve/')) return json({ order_id: row.id, agent_session_id: null });
    if (path.endsWith('/chief-workspace')) return json([{ ...row, paused: true, status: 'running' }]);
    if (path.endsWith('/chief-workspace/order-1/progress')) return json(progress);
    if (path.endsWith('/team-session')) return json(view);
    return json([]);
  }));
  renderWorkspace(row.id);
  const card = await screen.findByRole('region', { name: 'Current work' });
  expect(within(card).getByRole('button', { name: 'Resume work' })).toBeEnabled();
});

it('opens guarded recovery for an unknown Chief attempt in the root session', async () => {
  const progress = { counts: { active_attempts: 0, queued_attempts: 0, unfinished_assignments: 0,
    blocked_assignments: 0, ready_artifacts: 0 }, paused: false,
    blocker: { kind: 'unknown_attempt', detail: 'Chief has an unverified outcome.', session_id: 'session-1', attempt_id: 'attempt-root' },
    next_action: { kind: 'open_agent', label: 'Review uncertain attempt', session_id: 'session-1' } };
  const rootView = { ...view, attempts: [{ id: 'attempt-root', session_id: 'session-1', status: 'unknown',
    unknown_acknowledged: false, reason: 'runner_interrupted' }] };
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.includes('/resolve/')) return json({ order_id: row.id, agent_session_id: null });
    if (path.endsWith('/chief-workspace')) return json([{ ...row, status: 'running', state: 'Checking outcome' }]);
    if (path.endsWith('/chief-workspace/order-1/progress')) return json(progress);
    if (path.endsWith('/team-session')) return json(rootView);
    return json([]);
  }));
  renderWorkspace(row.id);
  await userEvent.setup().click(await screen.findByRole('button', { name: 'Review uncertain attempt' }));
  expect(await screen.findByRole('region', { name: 'Agent recovery' })).toHaveTextContent('Unknown outcome needs your decision');
  expect(screen.getByRole('button', { name: 'Reconcile saved actions' })).toHaveFocus();
  expect(screen.queryByText('The affected agent could not be found')).not.toBeInTheDocument();
});

it('routes a text completion review action to the saved operator decision', async () => {
  const progress = { counts: { active_attempts: 0, queued_attempts: 0, unfinished_assignments: 0,
    blocked_assignments: 0, ready_artifacts: 0 }, paused: false, blocker: null,
    next_action: { kind: 'review_outcome', label: 'Review completed result' } };
  const reviewOrder = { id: row.id, project_id: project.id, title: row.title, instructions: 'Prepare a brief',
    profile_id: 'chief-1', deliverable_kind: 'text_reply', acceptance_checks: ['Confirm facts'],
    parent_work_order_id: null, source_run_id: null, origin_signal_id: null, chain_depth: 0,
    status: 'needs_review', version: 3, block_reason: null, blocked_from_status: null, created_at: '2026-10-18T12:00:00Z' };
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.includes('/resolve/')) return json({ order_id: row.id, agent_session_id: null });
    if (path.endsWith('/chief-workspace')) return json([{ ...row, status: 'needs_review', completion_policy: 'operator_review', state: 'Review requested' }]);
    if (path.endsWith('/chief-workspace/order-1/progress')) return json(progress);
    if (path.endsWith('/team-session')) return json(view);
    if (path.endsWith('/work-orders')) return json([reviewOrder]);
    return json([]);
  }));
  renderWorkspace(row.id);
  await userEvent.setup().click(await screen.findByRole('button', { name: 'Review completed result' }));
  expect(screen.getByRole('region', { name: 'Work order review' })).toHaveTextContent('Review completion');
  expect(screen.getByRole('textbox', { name: 'Reason for your decision' })).toHaveFocus();
});

it('lists distinct saved deliverables from the same run', async () => {
  const files = [
    { id: 'artifact-1', run_id: 'run-1', profile_id: 'research-1', profile_name: 'Researcher', kind: 'markdown', work_order_title: 'Market evidence summary' },
    { id: 'artifact-2', run_id: 'run-1', profile_id: 'research-1', profile_name: 'Researcher', kind: 'markdown' },
  ];
  const savedArtifacts = files.map((file) => ({ id: file.id, kind: 'markdown', mime_type: 'text/markdown',
    sha256: 'a'.repeat(64), byte_size: 20, status: 'ready', created_at: '2026-10-18T12:00:00Z', finished_at: '2026-10-18T12:01:00Z' }));
  const fetch = vi.fn(async (path: string) => {
    if (path.includes('/resolve/')) return json({ order_id: row.id, agent_session_id: null });
    if (path.endsWith('/chief-workspace')) return json([row]);
    if (path.endsWith('/team-session')) return json(view);
    if (path.endsWith('/team-session/artifacts')) return json(files);
    if (path.endsWith('/runs/run-1/workspace-artifacts')) return json(savedArtifacts);
    if (path.endsWith('/runs/run-1/workspace-artifacts/artifact-2')) return json({ ...savedArtifacts[1], content: 'Second saved deliverable' });
    return json([]);
  });
  vi.stubGlobal('fetch', fetch);
  renderWorkspace(row.id);
  const deliveries = await screen.findByRole('region', { name: 'Saved deliverables' });
  expect(within(deliveries).getAllByRole('button')).toHaveLength(2);
  expect(within(deliveries).getByRole('button', { name: /Market evidence summary/ })).toHaveTextContent('Researcher · markdown');
  await userEvent.setup().click(within(deliveries).getByRole('button', { name: /file 2/ }));
  expect(await screen.findByText('Second saved deliverable')).toBeInTheDocument();
  expect(fetch.mock.calls.some(([path]) => path.endsWith('/workspace-artifacts/artifact-2'))).toBe(true);
});

it('shows six readable recent files, all exact artifacts on request, and keeps a selected preview', async () => {
  const trace = 'f1e01ea2-6782-41fb-b84d-c6aa61d9c742';
  const longTitle = `REFRESH operating-cost handoff ${trace} now that public search is ready and the source evidence has changed for the DistrictAI pricing review`;
  const files = Array.from({ length: 8 }, (_, index) => ({ id: `artifact-${index + 1}`, run_id: 'run-1',
    profile_id: 'research-1', profile_name: 'Researcher', kind: 'markdown', status: 'ready', work_order_title: longTitle }));
  const savedArtifacts = files.map((file) => ({ id: file.id, kind: 'markdown', mime_type: 'text/markdown',
    sha256: 'a'.repeat(64), byte_size: 20, status: 'ready', created_at: '2026-10-18T12:00:00Z', finished_at: '2026-10-18T12:01:00Z' }));
  const fetch = vi.fn(async (path: string) => {
    if (path.includes('/resolve/')) return json({ order_id: row.id, agent_session_id: null });
    if (path.endsWith('/chief-workspace')) return json([row]);
    if (path.endsWith('/team-session')) return json(view);
    if (path.endsWith('/team-session/artifacts')) return json(files);
    if (path.endsWith('/runs/run-1/workspace-artifacts')) return json(savedArtifacts);
    if (path.endsWith('/runs/run-1/workspace-artifacts/artifact-8')) return json({ ...savedArtifacts[7], content: 'Exact eighth deliverable' });
    return json([]);
  });
  vi.stubGlobal('fetch', fetch);
  renderWorkspace(row.id);
  const deliveries = await screen.findByRole('region', { name: 'Saved deliverables' });
  expect(within(deliveries).getAllByRole('button', { name: /Open saved file/ })).toHaveLength(6);
  expect(within(deliveries).getAllByText(/REFRESH operating-cost handoff/)[0]).not.toHaveTextContent(trace);
  expect(within(deliveries).queryByRole('button', { name: new RegExp(trace) })).not.toBeInTheDocument();
  await userEvent.setup().click(within(deliveries).getAllByText('Full task title')[0]);
  expect(within(deliveries).getAllByText(longTitle)[0]).toBeVisible();
  expect(within(deliveries).getByRole('button', { name: 'Show all 8 files' })).toHaveAttribute('aria-expanded', 'false');
  await userEvent.setup().click(within(deliveries).getByRole('button', { name: 'Show all 8 files' }));
  expect(within(deliveries).getAllByRole('button', { name: /Open saved file/ })).toHaveLength(8);
  await userEvent.setup().click(within(deliveries).getByRole('button', { name: /Open saved file 8/ }));
  expect(await screen.findByText('Exact eighth deliverable')).toBeInTheDocument();
  await userEvent.setup().click(within(deliveries).getByRole('button', { name: 'Show recent 6 files' }));
  expect(within(deliveries).getAllByRole('button', { name: /Open saved file/ })).toHaveLength(6);
  expect(screen.getByText('Exact eighth deliverable')).toBeInTheDocument();
  expect(fetch.mock.calls.some(([path]) => path.endsWith('/workspace-artifacts/artifact-8'))).toBe(true);
});

it('requires a reasoned operator decision for an exact unknown child attempt', async () => {
  const child = { id: 'child-unknown', profile_id: 'research-1', work_order_id: 'child-order', status: 'closed', parent_session_id: 'session-1' };
  const acknowledgement = vi.fn();
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.includes('/resolve/')) return json({ order_id: row.id, agent_session_id: null });
    if (path.endsWith('/chief-workspace')) return json([row]);
    if (path.endsWith('/team-session')) return json({ ...view, children: [child], assignments: [{ id: 'assignment-1', child_session_id: child.id,
      objective: 'Research sources', expected_result: 'Saved findings', acceptance_check: 'Cite sources', status: 'satisfied' }] });
    if (path.endsWith('/sessions/child-unknown')) return json({ ...view, session: { ...view.session, id: child.id },
      attempts: [{ id: 'attempt-unknown', session_id: child.id, status: 'unknown', unknown_acknowledged: false,
        reason: 'runner_interrupted', created_at: '2026-10-18T12:00:00Z', finished_at: '2026-10-18T12:01:00Z' }] });
    if (path.endsWith('/sessions/child-unknown/reconcile')) return json({ reconciled_actions: 0 });
    if (path.endsWith('/attempts/attempt-unknown/acknowledge')) { acknowledgement(JSON.parse(String(init?.body))); return json({}); }
    return json([]);
  }));
  renderWorkspace(row.id);
  await userEvent.setup().click(await screen.findByText('Past agent work (1)'));
  await userEvent.setup().click(screen.getByRole('button', { name: 'Open Agent: Research sources (Past)' }));
  const panel = screen.getByRole('dialog', { name: 'Agent' });
  expect(await within(panel).findByText(/Unknown outcome needs your decision/)).toBeInTheDocument();
  expect(acknowledgement).not.toHaveBeenCalled();
  const reason = within(panel).getByRole('textbox', { name: /Reason for abandoning/ });
  await userEvent.setup().click(within(panel).getByRole('button', { name: 'Reconcile saved actions' }));
  await within(panel).findByText('0 saved action receipts reconciled. No agent turn was launched.');
  await userEvent.setup().type(reason, 'No confirmed result or outstanding effects were found.');
  await userEvent.setup().click(within(panel).getByRole('button', { name: 'Abandon unverified result' }));
  await waitFor(() => expect(acknowledgement).toHaveBeenCalledOnce());
  expect(acknowledgement.mock.calls[0]?.[0]).toMatchObject({ decision: 'abandon_unverified', reason: 'No confirmed result or outstanding effects were found.' });
});

it('keeps delegated raw result text in the agent panel while Chief chat shows its handoff', async () => {
  const child = { id: 'child-1', profile_id: 'research-1', work_order_id: 'child-order', status: 'closed', parent_session_id: 'session-1' };
  const childResult = 'Saved source report. '.repeat(200);
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.includes('/resolve/')) return json({ order_id: row.id, agent_session_id: null });
    if (path.endsWith('/chief-workspace')) return json([row]);
    if (path.endsWith('/team-session')) return json({ ...view, children: [child], messages: [view.messages[0],
      { ...view.messages[0], id: 'handoff', sequence: 2, kind: 'agent', actor_id: 'profile:research-1', source_session_id: child.id, body: childResult }] });
    if (path.endsWith('/sessions/child-1')) return json({ ...view, session: { ...view.session, id: child.id }, messages: [
      { ...view.messages[0], id: 'result', kind: 'agent', body: childResult }] });
    return json([]);
  }));
  renderWorkspace(row.id);
  const conversation = await screen.findByRole('region', { name: 'Chief conversation' });
  expect(within(conversation).queryByText(childResult)).not.toBeInTheDocument();
  await userEvent.setup().click(await within(conversation).findByRole('button', { name: 'Open Agent result' }));
  const panel = screen.getByRole('dialog', { name: 'Agent' });
  await userEvent.setup().click(within(panel).getByRole('tab', { name: 'Conversation' }));
  expect(await within(panel).findByText(/Saved source report\. Saved source report\./)).toBeInTheDocument();
});

it('retains an unresolved first-message identity across a reload and a missing lookup', async () => {
  const body = 'Prepare one customer demonstration.';
  const bytes = new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(body)));
  const hash = [...bytes].map((byte) => byte.toString(16).padStart(2, '0')).join('');
  const id = crypto.randomUUID();
  window.sessionStorage.setItem('onefoundry:chief-message:project-1', JSON.stringify({ id, hash, orderId: null }));
  const requests: Record<string, unknown>[] = [];
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.includes('/chief-workspace/messages/')) return json({ detail: 'Not recorded yet' }, 404);
    if (path.endsWith('/chief-workspace/messages')) {
      requests.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
      return json({ order_id: row.id, session_id: 'session-1', message_id: 'saved-1', attempt_id: 'attempt-1', sequence: 1, disposition: 'queued', detail: null }, 202);
    }
    if (path.endsWith('/chief-workspace')) return json([]);
    if (path.endsWith('/team-session')) return json(view);
    return json([]);
  }));
  const user = userEvent.setup(); renderWorkspace();
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message Chief' })).toBeEnabled());
  await user.type(screen.getByRole('textbox', { name: 'Message Chief' }), body);
  await user.click(screen.getByRole('button', { name: /Send/ }));
  await waitFor(() => expect(requests).toHaveLength(1));
  expect(requests[0]?.client_message_id).toBe(id);
});

it('opens archived messages as history and moves them to recoverable Trash', async () => {
  let saved = { ...row, archived: true, trashed: false };
  const controls: Record<string, unknown>[] = [];
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.includes('/resolve/')) return json({ order_id: row.id, agent_session_id: null });
    if (path.endsWith('/controls')) {
      const data = JSON.parse(String(init?.body)) as Record<string, unknown>; controls.push(data);
      saved = { ...saved, trashed: data.action === 'trash', paused: true, controls_version: saved.controls_version + 1 };
      return json({ order_id: row.id, action: data.action, controls_version: saved.controls_version });
    }
    if (path.endsWith('/chief-workspace')) return json([saved]);
    if (path.endsWith('/team-session')) return json(view);
    return json([]);
  }));
  const user = userEvent.setup(); renderWorkspace(row.id);
  await screen.findAllByText('Create the brief.');
  expect(screen.queryByRole('textbox', { name: 'Message Chief' })).not.toBeInTheDocument();
  await user.click(screen.getByLabelText('Conversation actions'));
  await user.click(screen.getByRole('button', { name: 'Delete conversation' }));
  await screen.findByText('Conversation moved to Trash.');
  await user.click(screen.getByRole('button', { name: 'Archived' }));
  expect(await screen.findByText('No archived conversations.')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Trash' }));
  await user.click(screen.getByRole('button', { name: 'Restore Product brief from Trash' }));
  await screen.findByText('Restored to Archived. Future chat turns remain paused.');
  expect(controls.map((data) => data.action)).toEqual(['trash', 'restore']);
});

it('keeps archived history visible when its deletion is rejected by the owner', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.includes('/resolve/')) return json({ order_id: row.id, agent_session_id: null });
    if (path.endsWith('/controls')) return json({ detail: 'The current turn must finish.' }, 409);
    if (path.endsWith('/chief-workspace')) return json([{ ...row, archived: true }]);
    if (path.endsWith('/team-session')) return json(view);
    return json([]);
  }));
  const user = userEvent.setup(); renderWorkspace(row.id);
  await screen.findAllByText('Create the brief.');
  await user.click(screen.getByLabelText('Conversation actions'));
  await user.click(screen.getByRole('button', { name: 'Delete conversation' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('The current turn must finish.');
  expect(screen.getAllByText('Create the brief.').length).toBeGreaterThan(0);
});

it('does not open or create a session from an old link to a trashed conversation', async () => {
  const fetcher = vi.fn(async (path: string) => {
    if (path.includes('/resolve/')) return json({ detail: 'Conversation not found in this project.' }, 404);
    if (path.endsWith('/chief-workspace')) return json([{ ...row, archived: true, paused: true, trashed: true }]);
    return json([]);
  });
  vi.stubGlobal('fetch', fetcher); renderWorkspace(row.id);
  await screen.findByRole('alert');
  expect(screen.getByRole('combobox', { name: 'Conversation agent' })).toHaveValue('');
  expect(screen.getByRole('textbox', { name: 'Message Chief' })).toBeInTheDocument();
  expect(fetcher.mock.calls.every(([path]) => !path.includes('/team-session'))).toBe(true);
});

it('loads scoped capability checks in the primary conversation only after opening Details', async () => {
  const paths: string[] = [];
  const writes: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    paths.push(path);
    if (init?.method && init.method !== 'GET') writes.push(path);
    if (path.includes('/resolve/')) return json({ order_id: row.id, agent_session_id: 'session-1' });
    if (path.endsWith('/chief-workspace')) return json([row]);
    if (path.endsWith('/team-session')) return json(view);
    if (path.endsWith('/readiness')) return json({ work_order_id: row.id, status: 'failed', execution_mode: 'team', worker_enabled: true,
      provider: null, ready_to_activate: false, blockers: [], pending_approvals: 0, queued_attempts: 0, running_attempts: 0,
      confirmed_results: 0, next_step: 'Inspect saved state', capability_readiness: { project_id: project.id, profile_id: 'chief-1',
        observed_at: '2026-10-12T12:00:00Z', boundary: 'Preflight is not execution', omitted_sources: 0,
        checks: [{ operation: 'repository_read', title: 'Read selected repository', owner: 'source_store', source_id: null, revision: null,
          status: 'configuration_missing', next_action: 'configure', blockers: [{ kind: 'configuration_missing', code: 'source_unavailable',
            owner: 'source_store', message: 'Selected repository is unavailable.', next_step: 'Mount the exact repository read-only.' }] }] } });
    return json([]);
  }));
  const user = userEvent.setup(); renderWorkspace(row.id);
  await screen.findByRole('heading', { name: 'Product brief' });
  const details = await screen.findByText('Details & earlier attempts');
  expect(paths.some((path) => path.endsWith('/readiness'))).toBe(false);
  await user.click(details);
  await user.click(await screen.findByText('Capability readiness'));
  expect(await screen.findByText('Mount the exact repository read-only.')).toBeVisible();
  expect(paths.filter((path) => path.endsWith('/readiness'))).toEqual(['/api/projects/project-1/work-orders/order-1/team-session/readiness']);
  expect(writes).toEqual([]);
});
