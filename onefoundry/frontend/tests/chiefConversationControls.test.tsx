import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ChiefWorkspace } from '../src/ChiefWorkspace';
import type { Project } from '../src/api';

const row = { id: 'order-1', title: 'Current brief', status: 'failed', version: 3, profile_id: 'chief-1',
  profile_name: 'Chief of Staff', archived: false, paused: false, controls_version: 1, canonical_chief: true,
  completion_policy: 'chief_verified', state: 'Stopped' };
const other = { ...row, id: 'order-2', title: 'Earlier brief', archived: true };
const view = { session: { id: 'session-1', profile_id: 'chief-1', status: 'open', execution_mode: 'team' },
  messages: [], attempts: [], children: [], assignments: [], decision_causes: [], tool_calls: [] };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
afterEach(() => { vi.unstubAllGlobals(); window.sessionStorage.clear(); });

it('archives an unselected chat through its exact owner and preserves the current draft', async () => {
  const controls: { path: string; data: Record<string, unknown> }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.includes('/resolve/')) return json({ order_id: row.id, agent_session_id: null });
    if (path.endsWith('/controls')) {
      const data = JSON.parse(String(init?.body)) as Record<string, unknown>;
      controls.push({ path, data });
      return json({ order_id: other.id, action: data.action, controls_version: 2 });
    }
    if (path.endsWith('/chief-workspace')) return json([row, { ...other, archived: controls.length > 0 }]);
    if (path.endsWith('/team-session')) return json(view);
    return json([]);
  }));
  render(<ChiefWorkspace project={{ id: 'project-1', name: 'Studio' } as Project} profiles={[]}
    initialOrderId={row.id} onSelect={vi.fn()} onMenu={vi.fn()} onNavigate={vi.fn()} />);
  const user = userEvent.setup();
  const archive = await screen.findByRole('button', { name: 'Archive Earlier brief' });
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message Chief' })).toBeEnabled());
  await user.type(screen.getByRole('textbox', { name: 'Message Chief' }), 'Unsent current draft');
  await user.click(archive);
  await screen.findByText('Conversation archived. Its work and history remain available.');
  expect(controls).toHaveLength(1);
  expect(controls[0]?.path).toContain('/chief-workspace/order-2/controls');
  expect(controls[0]?.data).toMatchObject({ action: 'archive', expected_version: 3, expected_controls_version: 1 });
  expect(screen.getByRole('textbox', { name: 'Message Chief' })).toHaveValue('Unsent current draft');
});

it('deletes a different archived chat without clearing the selected conversation or its draft', async () => {
  let trashed = false;
  const selected = vi.fn();
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.includes('/resolve/')) return json({ order_id: row.id, agent_session_id: null });
    if (path.endsWith('/controls')) {
      const data = JSON.parse(String(init?.body)) as Record<string, unknown>;
      trashed = true;
      return json({ order_id: other.id, action: data.action, controls_version: 2 });
    }
    if (path.endsWith('/chief-workspace')) return json([row, { ...other, trashed }]);
    if (path.endsWith('/team-session')) return json(view);
    return json([]);
  }));
  render(<ChiefWorkspace project={{ id: 'project-1', name: 'Studio' } as Project} profiles={[]}
    initialOrderId={row.id} onSelect={selected} onMenu={vi.fn()} onNavigate={vi.fn()} />);
  const user = userEvent.setup();
  await screen.findByRole('heading', { name: row.title });
  await user.type(screen.getByRole('textbox', { name: 'Message Chief' }), 'Keep this draft');
  await user.click(screen.getByRole('button', { name: 'Archived' }));
  await user.click(screen.getByRole('button', { name: 'Move Earlier brief to Trash' }));
  await screen.findByText('Conversation moved to Trash.');
  expect(screen.getByRole('heading', { name: row.title })).toBeInTheDocument();
  expect(screen.getByRole('textbox', { name: 'Message Chief' })).toHaveValue('Keep this draft');
  expect(selected).not.toHaveBeenCalledWith(null);
});

it('starts another chat with action activity collapsed after expanding the previous chat', async () => {
  const second = { ...other, archived: false };
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.includes('/resolve/')) return json({ order_id: row.id, agent_session_id: null });
    if (path.endsWith('/chief-workspace')) return json([row, second]);
    if (path.endsWith('/team-session')) return json({ ...view,
      session: { ...view.session, id: path.includes('/order-2/') ? 'session-2' : 'session-1' },
      messages: [{ id: 'system-record', sequence: 1, kind: 'system', body: 'Saved context', created_at: '2026-10-15T12:00:00Z' }] });
    return json([]);
  }));
  render(<ChiefWorkspace project={{ id: 'project-1', name: 'Studio' } as Project} profiles={[]}
    initialOrderId={row.id} onSelect={vi.fn()} onMenu={vi.fn()} onNavigate={vi.fn()} />);
  const user = userEvent.setup();
  await user.click(await screen.findByText('Details & earlier attempts'));
  await user.click(screen.getByRole('button', { name: 'Show agent action activity (1)' }));
  expect(screen.getByRole('button', { name: 'Hide agent action activity (1)' })).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Earlier brief Stopped' }));
  await screen.findByRole('heading', { name: second.title });
  await user.click(await screen.findByText('Details & earlier attempts'));
  expect(screen.getByRole('button', { name: 'Show agent action activity (1)' })).toBeInTheDocument();
});
