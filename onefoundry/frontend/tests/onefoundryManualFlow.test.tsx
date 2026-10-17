import { afterEach, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { OneFoundryOrganizationsView } from '../src/OneFoundryOrganizationsView';
import { OneFoundryDraftView } from '../src/OneFoundryDraftView';
import type { Detail } from '../src/onefoundryOrganizationApi';
import type { Profile } from '../src/api';

const projectId = 'project-1';
const orgId = 'org-1';
const organization = { id: orgId, project_id: projectId, display_name: 'Fictional Cedar District',
  canonical_domain: null, identity_reviewed: false, qualification: 'unreviewed' as const, revision: 1 };
const source = { id: 'source-1', source_kind: 'public_excerpt' as const, official_url: 'https://example.org/about',
  excerpt: 'Fictional Cedar District serves four schools.', observed_at: '2026-09-28T00:00:00Z',
  revision: 1, superseded_by: null };
const profile = { id: 'profile-1', name: 'Researcher' } as Profile;
const researchDetail: Detail = { organization, sources: [source], claims: [], workflow_revision: 2,
  next_action: { action_key: 'research', label: 'Research district', reason: 'Start a linked research order.',
    workflow_revision: 2, source_revision: 1 }, linked_order: null, context_packet: null, budget: null, drafts: [] };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body),
  { status, headers: { 'Content-Type': 'application/json' } });

afterEach(() => vi.unstubAllGlobals());

it('creates one linked research order and rereads the saved owner state', async () => {
  let linked = false;
  const calls: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    calls.push(path);
    if (path.endsWith('/company-statements')) return json([]);
    if (path.endsWith('/organizations')) return json([organization]);
    if (path.endsWith('/research-orders') && init?.method === 'POST') {
      linked = true; return json({ work_order_id: 'order-1', context_packet_id: 'packet-1' }, 201);
    }
    if (path.endsWith('/org-1')) return json(linked ? { ...researchDetail, workflow_revision: 3,
      next_action: { ...researchDetail.next_action, action_key: 'start_research', label: 'Start research' },
      linked_order: { work_order_id: 'order-1', step_key: 'research', status: 'ready', run_id: null, run_status: null } } : researchDetail);
    throw new Error(`Unexpected path ${path}`);
  }));
  const user = userEvent.setup();
  render(<OneFoundryOrganizationsView projectId={projectId} mode="manual" profiles={[profile]} />);
  await user.click(await screen.findByRole('button', { name: organization.display_name }));
  await user.selectOptions(await screen.findByLabelText('Profile for this order'), profile.id);
  await user.click(screen.getByRole('button', { name: 'Research district' }));
  expect(await screen.findByRole('button', { name: 'Start research' })).toBeInTheDocument();
  expect(calls.some((path) => path.endsWith('/research-orders'))).toBe(true);
});

it('shows unknown outcome without a resend control', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.endsWith('/company-statements')) return json([]);
    if (path.endsWith('/organizations')) return json([organization]);
    if (path.endsWith('/org-1')) return json({ ...researchDetail,
      next_action: { ...researchDetail.next_action, action_key: 'reconcile', label: 'Reconcile',
        reason: 'The prior outcome is unknown.' },
      linked_order: { work_order_id: 'order-1', step_key: 'research', status: 'unknown',
        run_id: 'run-1', run_status: 'unknown' } });
    throw new Error(`Unexpected path ${path}`);
  }));
  const user = userEvent.setup();
  render(<OneFoundryOrganizationsView projectId={projectId} mode="manual" profiles={[profile]} />);
  await user.click(await screen.findByRole('button', { name: organization.display_name }));
  expect(await screen.findByText(/No resend is available/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Start research' })).not.toBeInTheDocument();
});

it('saves a cited sentence and displays its exact reference', async () => {
  const claim = { id: 'claim-1', source_id: source.id, claim_text: source.excerpt!, locator: 'About',
    review_state: 'approved' as const, verification_state: 'not_run' as const, revision: 2, fresh: true };
  const detail: Detail = { ...researchDetail, claims: [claim],
    organization: { ...organization, qualification: 'qualified' },
    linked_order: { work_order_id: 'order-2', step_key: 'draft', status: 'ready', run_id: null, run_status: null },
    context_packet: { id: 'packet-1', sha256: 'abc', claim_ids: [claim.id], statement_versions: [], excluded: [] },
    drafts: [] };
  let saved = false;
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/company-statements')) return json([]);
    if (path.endsWith('/drafts') && init?.method === 'POST') { saved = true;
      return json({ id: 'draft-1', context_packet_id: 'packet-1', revision: 1,
        segments: [{ kind: 'district_fact', text: 'Your district serves four schools.', claim_id: claim.id }] }, 201);
    }
    if (path.endsWith('/org-1')) return json({ ...detail, drafts: saved ? [{ id: 'draft-1', context_packet_id: 'packet-1',
      revision: 1, segments: [{ kind: 'district_fact', text: 'Your district serves four schools.', claim_id: claim.id }] }] : [] });
    throw new Error(`Unexpected path ${path}`);
  }));
  const user = userEvent.setup();
  render(<OneFoundryDraftView projectId={projectId} detail={detail} onSaved={vi.fn()} />);
  await user.selectOptions(screen.getByLabelText('Sentence type'), 'district_fact');
  await user.type(screen.getByLabelText('Sentence'), 'Your district serves four schools.');
  await user.selectOptions(screen.getByLabelText('Supporting reference'), claim.id);
  await user.click(screen.getByRole('button', { name: 'Add sentence' }));
  await user.click(screen.getByRole('button', { name: 'Save cited draft' }));
  expect(saved).toBe(true);
});
