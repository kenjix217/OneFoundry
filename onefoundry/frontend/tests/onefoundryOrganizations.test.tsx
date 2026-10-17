import { afterEach, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { App } from '../src/App';
import { OneFoundryOrganizationsView } from '../src/OneFoundryOrganizationsView';

const project = { id: 'project-1', name: 'Studio', goal: '', desired_outcome: '', status: 'active', version: 1, trash_from_status: null };
const organization = { id: 'org-1', project_id: project.id, display_name: 'Fictional Grove District', canonical_domain: null,
  identity_reviewed: false, qualification: 'unreviewed', revision: 1 };
const action = { action_key: 'add_source', label: 'Add source', reason: 'Add an observed source.', workflow_revision: 1, source_revision: null };
const detail = { organization, sources: [] as unknown[], claims: [] as unknown[], workflow_revision: 1, next_action: action };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
afterEach(() => { vi.unstubAllGlobals(); window.history.replaceState(null, '', '/'); });

it.each(['off', 'records', 'manual'])('shows Organizations navigation only in %s mode', async (mode) => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path === '/api/onefoundry/business-mode') return json({ mode });
    if (path === '/api/projects') return json([project]);
    if (path === `/api/projects/${project.id}`) return json(project);
    if (path === `/api/projects/${project.id}/profiles`) return json([]);
    if (path.endsWith('/model/accounts') || path.endsWith('/model/providers') || path.endsWith('/model/catalog') ||
        path.endsWith('/work-orders') || path.endsWith('/runs') || path.endsWith('/sources')) return json([]);
    throw new Error(`Unexpected path ${path}`);
  }));
  render(<App />);
  await screen.findByRole('combobox', { name: 'Conversation agent' });
  await userEvent.setup().click(screen.getByRole('button', { name: 'Settings' }));
  if (mode !== 'off') expect(await screen.findByRole('option', { name: 'Organizations' })).toBeInTheDocument();
  else expect(screen.queryByRole('option', { name: 'Organizations' })).not.toBeInTheDocument();
});

it('rereads a created record and retains it after remount', async () => {
  let saved = false;
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `/api/projects/${project.id}/organizations` && init?.method === 'POST') { saved = true; return json(organization, 201); }
    if (path === `/api/projects/${project.id}/organizations`) return json(saved ? [organization] : []);
    if (path.endsWith('/org-1')) return json(detail);
    throw new Error(`Unexpected path ${path}`);
  }));
  const user = userEvent.setup();
  const view = render(<OneFoundryOrganizationsView projectId={project.id} />);
  await user.type(screen.getByLabelText('District name'), organization.display_name);
  await user.click(screen.getByRole('button', { name: 'Add district' }));
  expect(await screen.findByRole('heading', { name: organization.display_name })).toBeInTheDocument();
  view.unmount();
  render(<OneFoundryOrganizationsView projectId={project.id} />);
  expect(await screen.findByRole('button', { name: organization.display_name })).toBeInTheDocument();
});

it('shows a failed save and keeps input for a deliberate retry', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/organizations') && init?.method === 'POST') return json({ detail: 'database unavailable' }, 503);
    if (path.endsWith('/organizations')) return json([]);
    throw new Error(`Unexpected path ${path}`);
  }));
  const user = userEvent.setup();
  render(<OneFoundryOrganizationsView projectId={project.id} />);
  await user.type(screen.getByLabelText('District name'), 'Fixture District');
  await user.click(screen.getByRole('button', { name: 'Add district' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('database unavailable');
  expect(screen.getByLabelText('District name')).toHaveValue('Fixture District');
});

it('shows stale revision response and current saved detail', async () => {
  const original = { ...detail, sources: [{ id: 'source-1', source_kind: 'public_excerpt', official_url: 'https://example.org',
    excerpt: 'Observed line', observed_at: '2026-09-28T00:00:00Z', revision: 1, superseded_by: null }],
    next_action: { ...action, action_key: 'review_source', label: 'Review source' } };
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/organizations')) return json([organization]);
    if (path.endsWith('/org-1/sources') && init?.method === 'POST') return json({ detail: 'workflow revision changed' }, 409);
    if (path.endsWith('/org-1')) return json(original);
    throw new Error(`Unexpected path ${path}`);
  }));
  const user = userEvent.setup();
  render(<OneFoundryOrganizationsView projectId={project.id} />);
  await user.click(await screen.findByRole('button', { name: organization.display_name }));
  await user.type(await screen.findByLabelText('Official HTTPS URL'), 'https://example.org/about');
  await user.type(screen.getByLabelText('Observed excerpt'), 'Another line');
  await user.click(screen.getByRole('button', { name: 'Save source' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('workflow revision changed');
  expect(screen.getByLabelText('Observed excerpt')).toHaveValue('Another line');
});
