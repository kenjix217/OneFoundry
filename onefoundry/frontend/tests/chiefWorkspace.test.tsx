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
  await user.click(await screen.findByRole('button', { name: /Agent ↗/ }));
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
