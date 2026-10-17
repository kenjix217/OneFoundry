import { afterEach, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { OneFoundryOperatorInboxView } from '../src/OneFoundryOperatorInboxView';

const projectId = 'project-inbox';
const action = { action_key: 'review_claims', label: 'Review claims', reason: 'Review evidence.', workflow_revision: 2, source_revision: 1 };
const item = { subject_ref: { kind: 'organization', id: 'org-1' }, subject_name: 'Fictional Harbor District', action,
  reason: action.reason, required_actor: 'operator', due_at: null, blocked_reason: null,
  evidence_state: 'candidate', work_order_id: null, work_order_status: null, approval_pending: false };
const empty = { actionable: [], waiting: [], blocked: [], unknown: [] };
const outcomes = { reviewed_research: 0, qualified_districts: 0, saved_drafts: 0,
  districts_with_drafts: 0, sent: 0, sent_basis: 'No delivery receipt owner.', observed_tokens: null, observed_cost_usd: null };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body),
  { status, headers: { 'Content-Type': 'application/json' } });
afterEach(() => vi.unstubAllGlobals());

it('shows an empty project and unavailable usage', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => json(path.endsWith('/inbox') ? empty : outcomes)));
  render(<OneFoundryOperatorInboxView projectId={projectId} onOpenOrganization={vi.fn()} onNavigate={vi.fn()} />);
  expect(await screen.findByRole('heading', { name: 'Saved outcomes' })).toBeInTheDocument();
  expect(screen.getAllByText('Unavailable')).toHaveLength(2);
  expect(screen.getAllByText('No districts in this group.')).toHaveLength(4);
  expect(screen.getByRole('button', { name: 'Work orders' })).toBeInTheDocument();
});

it('keeps related sections keyboard reachable', async () => {
  const onNavigate = vi.fn();
  vi.stubGlobal('fetch', vi.fn(async (path: string) => json(path.endsWith('/inbox') ? empty : outcomes)));
  const user = userEvent.setup();
  render(<OneFoundryOperatorInboxView projectId={projectId} onOpenOrganization={vi.fn()} onNavigate={onNavigate} />);
  await screen.findByRole('heading', { name: 'Saved outcomes' });
  await user.tab();
  expect(screen.getByRole('button', { name: 'Refresh inbox' })).toHaveFocus();
  await user.tab();
  expect(screen.getByRole('button', { name: 'Team' })).toHaveFocus();
  await user.keyboard('{Enter}');
  expect(onNavigate).toHaveBeenCalledWith('team');
});

it('keeps mixed states separate and checks saved revision before opening', async () => {
  const onOpenOrganization = vi.fn();
  const groups = { actionable: [item], waiting: [{ ...item, subject_ref: { kind: 'organization', id: 'org-2' }, subject_name: 'Waiting District' }],
    blocked: [], unknown: [] };
  let currentRevision = 3;
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.endsWith('/inbox')) return json(groups);
    if (path.endsWith('/outcomes')) return json(outcomes);
    if (path.endsWith('/org-1')) return json({ organization: { id: 'org-1', project_id: projectId, display_name: item.subject_name,
      qualification: 'unreviewed', revision: 1 }, sources: [], claims: [], workflow_revision: currentRevision,
      next_action: { ...action, workflow_revision: currentRevision } });
    throw new Error(`Unexpected path ${path}`);
  }));
  const user = userEvent.setup();
  render(<OneFoundryOperatorInboxView projectId={projectId} onOpenOrganization={onOpenOrganization} onNavigate={vi.fn()} />);
  expect(await screen.findByText(item.subject_name)).toBeInTheDocument();
  expect(screen.getByText('Waiting District')).toBeInTheDocument();
  await user.click(screen.getAllByRole('button', { name: 'Open district' })[0]);
  expect(await screen.findByRole('alert')).toHaveTextContent('changed');
  expect(onOpenOrganization).not.toHaveBeenCalled();
  currentRevision = 2;
  await user.click(screen.getByRole('button', { name: 'Refresh inbox' }));
  await screen.findByText(item.subject_name);
  await user.click(screen.getAllByRole('button', { name: 'Open district' })[0]);
  expect(onOpenOrganization).toHaveBeenCalledWith('org-1');
});

it('shows a 503 as unavailable and offers a retry', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => path.endsWith('/inbox')
    ? json({ detail: 'database unavailable' }, 503) : json(outcomes)));
  render(<OneFoundryOperatorInboxView projectId={projectId} onOpenOrganization={vi.fn()} onNavigate={vi.fn()} />);
  expect(await screen.findByRole('alert')).toHaveTextContent('database unavailable');
  expect(screen.queryByRole('heading', { name: 'Saved outcomes' })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
});
