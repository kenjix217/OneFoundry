import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ChiefWorkspace } from '../src/ChiefWorkspace';
import type { Profile, Project } from '../src/api';

const project = { id: 'project-1', name: 'Studio' } as Project;
const profiles = [
  { id: 'chief-1', name: 'Chief of Staff', configured: true },
  { id: 'research-1', name: 'Researcher', configured: true },
  { id: 'custom-1', name: 'Product writer', configured: false },
] as Profile[];
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
afterEach(() => { vi.unstubAllGlobals(); window.sessionStorage.clear(); });

it('opens directly with a focused composer and Chief selected without creating any record', async () => {
  const fetcher = vi.fn<(path: string, init?: RequestInit) => Promise<Response>>(async () => json([])); vi.stubGlobal('fetch', fetcher);
  render(<ChiefWorkspace project={project} profiles={profiles} initialOrderId={null} onSelect={vi.fn()} onMenu={vi.fn()} onNavigate={vi.fn()} />);
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message Chief' })).toHaveFocus());
  expect(screen.getByRole('combobox', { name: 'Conversation agent' })).toHaveValue('');
  expect(screen.queryByRole('heading', { name: 'Talk to Chief' })).not.toBeInTheDocument();
  expect(fetcher.mock.calls.every((call) => !call[1] || call[1]?.method !== 'POST')).toBe(true);
});

it('switches directly to a project agent, preserves the draft, and sends to the selected profile', async () => {
  const requests: Record<string, unknown>[] = [];
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/chief-workspace/messages')) {
      requests.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
      return json({ order_id: 'new-order', session_id: 'new-session', message_id: 'new-message', attempt_id: null, sequence: 1, disposition: 'awaiting_setup', detail: 'Model setup is needed.' }, 202);
    }
    return json([]);
  }));
  const user = userEvent.setup();
  render(<ChiefWorkspace project={project} profiles={profiles} initialOrderId={null} onSelect={vi.fn()} onMenu={vi.fn()} onNavigate={vi.fn()} />);
  await user.type(screen.getByRole('textbox', { name: 'Message Chief' }), 'Explain the product research.');
  await user.selectOptions(screen.getByRole('combobox', { name: 'Conversation agent' }), 'research-1');
  expect(screen.getByRole('textbox', { name: 'Message Researcher' })).toHaveValue('Explain the product research.');
  expect(requests).toHaveLength(0);
  await user.click(screen.getByRole('button', { name: /Send/ }));
  await waitFor(() => expect(requests).toHaveLength(1));
  expect(requests[0]).toMatchObject({ profile_id: 'research-1', body: 'Explain the product research.' });
});

it('returns to Chief through the same dropdown without changing or creating saved work', async () => {
  const fetcher = vi.fn<(path: string, init?: RequestInit) => Promise<Response>>(async () => json([])); vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<ChiefWorkspace project={project} profiles={profiles} initialOrderId={null} onSelect={vi.fn()} onMenu={vi.fn()} onNavigate={vi.fn()} />);
  await user.selectOptions(screen.getByRole('combobox', { name: 'Conversation agent' }), 'custom-1');
  await user.type(screen.getByRole('textbox', { name: 'Message Product writer' }), 'An unsent question.');
  await user.selectOptions(screen.getByRole('combobox', { name: 'Conversation agent' }), '');
  expect(screen.getByRole('textbox', { name: 'Message Chief' })).toHaveValue('An unsent question.');
  expect(fetcher.mock.calls.every((call) => !call[1] || call[1]?.method !== 'POST')).toBe(true);
});

it('restores the selected agent and exact request after an unconfirmed send and reload', async () => {
  const requests: Record<string, unknown>[] = [];
  let disconnected = true;
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/chief-workspace/messages')) {
      requests.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
      if (disconnected) throw new Error('Connection lost');
      return json({ order_id: 'new-order', session_id: 'new-session', message_id: 'new-message', attempt_id: null, sequence: 1, disposition: 'awaiting_setup', detail: 'Model setup is needed.' }, 202);
    }
    if (path.includes('/chief-workspace/messages/')) return json({ detail: 'Not recorded' }, 404);
    return json([]);
  }));
  const user = userEvent.setup();
  const props = { project, profiles, initialOrderId: null, onSelect: vi.fn(), onMenu: vi.fn(), onNavigate: vi.fn() };
  const first = render(<ChiefWorkspace {...props} />);
  await user.selectOptions(screen.getByRole('combobox', { name: 'Conversation agent' }), 'research-1');
  await user.type(screen.getByRole('textbox', { name: 'Message Researcher' }), 'Explain the product research.');
  await user.click(screen.getByRole('button', { name: /Send/ }));
  await screen.findByText(/Keep this text/);
  first.unmount();
  disconnected = false;
  render(<ChiefWorkspace {...props} />);
  await screen.findByText(/Re-enter the same text/);
  expect(screen.getByRole('combobox', { name: 'Conversation agent' })).toHaveValue('research-1');
  expect(screen.getByRole('combobox', { name: 'Conversation agent' })).toBeDisabled();
  await user.type(screen.getByRole('textbox', { name: 'Message Researcher' }), 'Explain the product research.');
  await user.click(screen.getByRole('button', { name: /Send/ }));
  await waitFor(() => expect(requests).toHaveLength(2));
  expect(requests[1]).toEqual(requests[0]);
});
