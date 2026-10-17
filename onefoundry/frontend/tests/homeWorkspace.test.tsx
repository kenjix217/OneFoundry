import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { HomeView } from '../src/HomeView';
import { defaultWorkspacePreferences } from '../src/workspacePreferences';
import type { Project } from '../src/api';

const project: Project = { id: 'project-1', name: 'Studio', goal: 'Launch the product', desired_outcome: 'Reviewed launch plan', status: 'active', version: 1, trash_from_status: null };
const order = { id: 'order-internal', project_id: project.id, title: 'Review launch plan', instructions: 'Review the saved draft', profile_id: 'agent-internal', status: 'needs_review', version: 1, block_reason: null, blocked_from_status: null, created_at: '2026-10-03T12:00:00Z', acceptance_checks: [], deliverable_kind: 'operator_review', parent_work_order_id: null, source_run_id: null, origin_signal_id: null, chain_depth: 0 };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
afterEach(() => vi.unstubAllGlobals());

it('opens the exact assignment from readable attention cards without internal IDs', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => json(path.endsWith('/work-orders') ? [order] : [])));
  const navigate = vi.fn();
  render(<HomeView project={project} profiles={[]} preferences={defaultWorkspacePreferences()} onNavigate={navigate} onCustomize={() => {}} />);
  const panel = screen.getByRole('region', { name: 'Needs your attention' });
  await userEvent.setup().click(await within(panel).findByRole('button', { name: /Review launch plan/ }));
  expect(navigate).toHaveBeenCalledWith('chat', order.id);
  expect(panel).not.toHaveTextContent(order.id);
  expect(panel).not.toHaveTextContent(order.profile_id);
});

it('shows failed reads as unavailable rather than an empty, healthy inbox', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => path.endsWith('/work-orders') ? json([]) : json({ detail: 'Unavailable' }, 503)));
  render(<HomeView project={project} profiles={[]} preferences={defaultWorkspacePreferences()} onNavigate={() => {}} onCustomize={() => {}} />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Approvals could not be loaded');
  expect(screen.queryByText('Nothing waiting on you')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: /Pending approvals/ })).toHaveTextContent('—');
});

it('honors hidden and reordered panels without removing inbox navigation', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => json([])));
  const prefs = { ...defaultWorkspacePreferences(), hiddenHomePanels: ['team' as const], homeOrder: ['work' as const, 'attention' as const, 'team' as const] };
  render(<HomeView project={project} profiles={[]} preferences={prefs} onNavigate={() => {}} onCustomize={() => {}} />);
  await screen.findByText('Nothing waiting on you');
  expect(screen.queryByRole('region', { name: 'Your team' })).not.toBeInTheDocument();
  expect(screen.getAllByRole('region').map((region) => region.getAttribute('aria-label')).filter(Boolean)).toEqual(['Recent work', 'Needs your attention']);
  expect(screen.getByRole('button', { name: /Pending approvals/ })).toBeVisible();
});
