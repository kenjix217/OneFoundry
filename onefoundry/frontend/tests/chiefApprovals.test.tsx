import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ChiefApprovals } from '../src/ChiefApprovals';
import type { Approval } from '../src/workApi';

const proposal: Approval = { id: 'approval-1', project_id: 'project-1', work_order_id: 'order-1', run_id: 'run-1',
  action_type: 'publish', target: 'Private demonstration', content: 'Publish this reviewed demonstration.',
  proposal_digest: 'saved-proposal', status: 'pending', created_at: '2026-10-07T12:00:00Z',
  decided_at: null, synthetic_merge: false };
afterEach(() => { vi.unstubAllGlobals(); });

it.each([['Approve', 'approved'], ['Decline', 'rejected']] as const)(
  'records %s in the Chief conversation and confirms the exact saved proposal', async (label, decision) => {
    const saved = { ...proposal, status: decision, decided_at: '2026-10-07T12:01:00Z' };
    const requests: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
      requests.push(path);
      if (path.endsWith('/decision')) {
        expect(JSON.parse(String(init?.body))).toEqual({ decision });
        return new Response(JSON.stringify(saved));
      }
      return new Response(JSON.stringify([saved]));
    }));
    const refreshed = vi.fn();
    render(<ChiefApprovals projectId="project-1" approvals={[proposal]} onSaved={refreshed} />);
    expect(screen.getByText(proposal.content, { selector: '.chief-message-body' })).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole('button', { name: label }));
    await waitFor(() => expect(refreshed).toHaveBeenCalledOnce());
    expect(requests).toEqual(['/api/projects/project-1/approvals/approval-1/decision', '/api/projects/project-1/approvals']);
    expect(screen.getByText(/Execution is tracked separately/)).toBeInTheDocument();
  },
);

it('keeps an unconfirmed decision visible when the owner readback has a different proposal', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => new Response(JSON.stringify(
    path.endsWith('/decision') ? { ...proposal, status: 'approved' } : [{ ...proposal, status: 'approved', proposal_digest: 'different-proposal' }],
  ))));
  const refreshed = vi.fn();
  render(<ChiefApprovals projectId="project-1" approvals={[proposal]} onSaved={refreshed} />);
  await userEvent.setup().click(screen.getByRole('button', { name: 'Approve' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('could not be confirmed');
  expect(refreshed).not.toHaveBeenCalled();
});
