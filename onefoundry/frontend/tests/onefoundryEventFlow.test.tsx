import { afterEach, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { OneFoundryOrganizationsView } from '../src/OneFoundryOrganizationsView';
import type { Detail } from '../src/onefoundryOrganizationApi';

const projectId = 'project-event';
const orgId = 'org-event';
const organization = { id: orgId, project_id: projectId, display_name: 'Fictional Bay District',
  canonical_domain: null, identity_reviewed: false, qualification: 'qualified' as const, revision: 2 };
const base: Detail = { organization, sources: [], claims: [], workflow_revision: 3,
  next_action: { action_key: 'pending_draft', label: 'Draft order pending', reason: 'Waiting for coordinator.',
    workflow_revision: 3, source_revision: null },
  linked_order: null, context_packet: null, budget: null, drafts: [],
  intent: { id: 'intent-1', state: 'pending', attempts: 0, created_at: '2026-09-29T12:00:00Z',
    lease_until: null, error_code: null, work_order_id: null } };
const json = (body: unknown) => new Response(JSON.stringify(body),
  { status: 200, headers: { 'Content-Type': 'application/json' } });

afterEach(() => vi.unstubAllGlobals());

it('shows the pending continuation without an automatic run control', async () => {
  let ready = false;
  const completed: Detail = { ...base,
    next_action: { ...base.next_action, action_key: 'start_draft', label: 'Start draft run' },
    intent: { ...base.intent!, state: 'done', work_order_id: 'order-1' },
    linked_order: { work_order_id: 'order-1', step_key: 'draft', status: 'ready', run_id: null, run_status: null } };
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.endsWith('/company-statements')) return json([]);
    if (path.endsWith('/organizations')) return json([organization]);
    if (path.endsWith(`/${orgId}`)) return json(ready ? completed : base);
    throw new Error(`Unexpected path ${path}`);
  }));
  const user = userEvent.setup();
  render(<OneFoundryOrganizationsView projectId={projectId} mode="event" />);
  await user.click(await screen.findByRole('button', { name: organization.display_name }));
  expect(await screen.findByText(/Draft continuation: pending/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Start draft run' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Draft outreach' })).not.toBeInTheDocument();
  ready = true;
  await user.click(screen.getByRole('button', { name: organization.display_name }));
  expect(await screen.findByRole('button', { name: 'Start draft run' })).toBeInTheDocument();
});

it('shows a terminal intent failure for operator review', async () => {
  const blocked: Detail = { ...base,
    next_action: { ...base.next_action, action_key: 'review_block', label: 'Review continuation' },
    intent: { ...base.intent!, state: 'blocked', error_code: 'evidence_stale' } };
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.endsWith('/company-statements')) return json([]);
    if (path.endsWith('/organizations')) return json([organization]);
    if (path.endsWith(`/${orgId}`)) return json(blocked);
    throw new Error(`Unexpected path ${path}`);
  }));
  const user = userEvent.setup();
  render(<OneFoundryOrganizationsView projectId={projectId} mode="event" />);
  await user.click(await screen.findByRole('button', { name: organization.display_name }));
  expect(await screen.findByText(/Draft continuation: blocked/)).toBeInTheDocument();
  expect(screen.getByText(/Reason: evidence_stale/)).toBeInTheDocument();
});
